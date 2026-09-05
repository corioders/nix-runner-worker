{
  config,
  lib,
  pkgs,
  ...
}:
let
  cfg = config.services.coriodersExternalWorker;
  scheduler = pkgs.callPackage ./scheduler/package.nix { };
  workerPackage = pkgs.callPackage ./worker/package.nix { };
  nativeRunner = import ./github-runner-native.nix { inherit lib pkgs; };
  darwinRunnerArchive = pkgs.fetchurl {
    url = "https://github.com/actions/runner/releases/download/v2.336.0/actions-runner-osx-arm64-2.336.0.tar.gz";
    hash = "sha256-jog5xJtwYLayFU9JMfgV3zMMJ/Fn1T7yI57j384osHk=";
  };
  darwinRunnerPackage = pkgs.runCommand "github-actions-runner-osx-arm64-2.336.0" { } ''
    mkdir -p "$out"
    ${pkgs.gnutar}/bin/tar -xzf ${darwinRunnerArchive} -C "$out"
  '';
  defaultRunnerPackage =
    if pkgs.stdenv.hostPlatform.isDarwin then darwinRunnerPackage else pkgs.github-runner;
  label = "corioders-worker-${cfg.name}";
  platformLabel = if pkgs.stdenv.hostPlatform.isDarwin then "macOS" else "Linux";
  architectureLabel = if pkgs.stdenv.hostPlatform.isAarch64 then "ARM64" else "X64";
  jobStartedHook = pkgs.writeShellApplication {
    name = "corioders-external-worker-job-started";
    runtimeInputs = [ scheduler ];
    text = ''
      exec corioders-runner-scheduler validate-event \
        --event-name "''${GITHUB_EVENT_NAME:?}" \
        --event-path "''${GITHUB_EVENT_PATH:?}"
    '';
  };
  workerCommands = lib.mapAttrs (
    scope: definition:
    nativeRunner.mkRunnerService {
      labels = [
        "self-hosted"
        platformLabel
        architectureLabel
        label
      ];
      namePrefix = "external-${cfg.name}-${scope}";
      inherit (definition) repository tokenFile;
      inherit (cfg) runnerGroupName runnerPackage;
      stateDirectory = "${cfg.stateDirectory}/workers/${scope}";
      jobStartedHook = "${jobStartedHook}/bin/corioders-external-worker-job-started";
    }
  ) cfg.scopes;
  workerPoolArguments = lib.concatStringsSep "\n" (
    lib.mapAttrsToList (
      scope: command:
      "scope_arguments+=(--scope-command ${lib.escapeShellArg "${scope}=${command}/bin/github-runner-external-${cfg.name}-${scope}"})"
    ) workerCommands
  );
  workerService = pkgs.writeShellApplication {
    name = "corioders-external-worker";
    runtimeInputs = [ workerPackage ];
    text = ''
      export CORIODERS_RUNNER_STATE_DIR=${lib.escapeShellArg cfg.stateDirectory}
      export CORIODERS_RUNNER_CAPACITY=${toString cfg.capacity}
      export CORIODERS_RUNNER_LEASE_TTL=${toString cfg.leaseTtlSeconds}
      scope_arguments=()
      ${workerPoolArguments}
      exec corioders-runner-worker-pool "''${scope_arguments[@]}"
    '';
  };
  target = pkgs.writeShellApplication {
    name = "corioders-external-runner-target";
    runtimeInputs = [ workerPackage ];
    text = ''
      export CORIODERS_RUNNER_STATE_DIR=${lib.escapeShellArg cfg.stateDirectory}
      export CORIODERS_RUNNER_CAPACITY=${toString cfg.capacity}
      export CORIODERS_RUNNER_LEASE_TTL=${toString cfg.leaseTtlSeconds}
      exec corioders-runner-target
    '';
  };
  schedulerPublicKey = lib.trim cfg.schedulerPublicKey;
  authorizedKey = ''restrict,command="${target}/bin/corioders-external-runner-target" ${schedulerPublicKey}'';
in
{
  options.services.coriodersExternalWorker = {
    enable = lib.mkEnableOption "Corioders external GitHub Actions worker target";
    name = lib.mkOption {
      type = lib.types.strMatching "[a-z0-9][a-z0-9-]*";
      description = "Stable worker name used in its GitHub Actions label.";
      example = "alice-m1";
    };
    capacity = lib.mkOption {
      type = lib.types.ints.positive;
      default = 1;
      description = "Maximum number of concurrent worker leases.";
    };
    leaseTtlSeconds = lib.mkOption {
      type = lib.types.ints.positive;
      default = 900;
      description = "Maximum age of a worker lease.";
    };
    runnerGroupName = lib.mkOption {
      type = lib.types.str;
      default = "corioders-self-hosted";
      description = "GitHub Actions runner group used by organization-scoped workers.";
    };
    runnerPackage = lib.mkOption {
      type = lib.types.package;
      default = defaultRunnerPackage;
      defaultText = lib.literalExpression "the official GitHub runner archive on Darwin, pkgs.github-runner on Linux";
      description = "GitHub Actions runner package.";
    };
    schedulerPublicKey = lib.mkOption {
      type = lib.types.nonEmptyStr;
      description = "Public key used by the trusted schedulers.";
    };
    stateDirectory = lib.mkOption {
      type = lib.types.str;
      default = "${config.home.homeDirectory}/.local/state/corioders-runner";
      defaultText = lib.literalExpression ''"\${config.home.homeDirectory}/.local/state/corioders-runner"'';
      description = "Private worker state and lease directory.";
    };
    scopes = lib.mkOption {
      description = "GitHub scopes this target can execute.";
      type = lib.types.attrsOf (
        lib.types.submodule {
          options = {
            repository = lib.mkOption {
              type = lib.types.nullOr lib.types.str;
              default = null;
              description = "Repository for a repository-scoped runner, or null for the organization.";
            };
            tokenFile = lib.mkOption {
              type = lib.types.str;
              description = "File containing the GitHub API token used to create JIT runners.";
            };
          };
        }
      );
    };
  };

  config = lib.mkIf cfg.enable {
    assertions = [
      {
        assertion = cfg.scopes != { };
        message = "services.coriodersExternalWorker.scopes must not be empty";
      }
      {
        assertion =
          lib.hasPrefix "ssh-ed25519 " schedulerPublicKey || lib.hasPrefix "ssh-rsa " schedulerPublicKey;
        message = "services.coriodersExternalWorker.schedulerPublicKey must be an SSH public key";
      }
      {
        assertion = lib.all (scope: builtins.match "[a-z0-9][a-z0-9-]*" scope != null) (
          builtins.attrNames cfg.scopes
        );
        message = "services.coriodersExternalWorker scope names may contain lowercase letters, digits, and hyphens only";
      }
      {
        assertion = pkgs.stdenv.hostPlatform.isLinux || pkgs.stdenv.hostPlatform.isDarwin;
        message = "coriodersExternalWorker supports Linux and Darwin only";
      }
      {
        assertion = !pkgs.stdenv.hostPlatform.isDarwin || pkgs.stdenv.hostPlatform.isAarch64;
        message = "external Darwin workers require Apple Silicon (aarch64-darwin)";
      }
    ];

    home.packages = [
      scheduler
      target
      workerPackage
      workerService
    ];

    home.activation.coriodersExternalWorker = lib.hm.dag.entryAfter [ "writeBoundary" ] ''
      run mkdir -p ${lib.escapeShellArg cfg.stateDirectory} "$HOME/.ssh"
      run chmod 700 ${lib.escapeShellArg cfg.stateDirectory} "$HOME/.ssh"
      authorized_keys="$HOME/.ssh/authorized_keys"
      filtered_keys="$(mktemp)"
      if [[ -e "$authorized_keys" ]]; then
        grep -Fv ' corioders-runner-external-scheduler' "$authorized_keys" >"$filtered_keys" || true
      fi
      printf '%s\n' ${lib.escapeShellArg "${authorizedKey} corioders-runner-external-scheduler"} >>"$filtered_keys"
      run install -m 600 "$filtered_keys" "$authorized_keys"
      rm -f "$filtered_keys"
    '';

    systemd.user.services.corioders-external-worker = lib.mkIf pkgs.stdenv.hostPlatform.isLinux {
      Unit.Description = "Corioders external GitHub Actions worker pool";
      Service = {
        ExecStart = "${workerService}/bin/corioders-external-worker";
        Restart = "always";
        RestartSec = 5;
      };
      Install.WantedBy = [ "default.target" ];
    };

    launchd.agents.corioders-external-worker = lib.mkIf pkgs.stdenv.hostPlatform.isDarwin {
      enable = true;
      config = {
        ProgramArguments = [ "${workerService}/bin/corioders-external-worker" ];
        KeepAlive = true;
        ProcessType = "Background";
        RunAtLoad = true;
        StandardErrorPath = "${cfg.stateDirectory}/worker-stderr.log";
        StandardOutPath = "${cfg.stateDirectory}/worker-stdout.log";
      };
    };
  };
}

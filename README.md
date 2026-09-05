# nix-runner-worker

Reusable Nix-managed GitHub Actions worker targets for Corioders. Scheduler
listeners stay on trusted infrastructure; enrolled Linux and Apple Silicon
Darwin machines expose only the restricted target protocol and an ephemeral
JIT worker pool.

Supported systems:

- `x86_64-linux`
- `aarch64-linux`
- `aarch64-darwin`

## Home Manager

Add the flake input and import its module:

```nix
{
  inputs.corioders-runner.url = "github:corioders/nix-runner-worker";

  outputs = { corioders-runner, home-manager, nixpkgs, ... }: {
    homeConfigurations."runner@alice-m1" =
      home-manager.lib.homeManagerConfiguration {
        pkgs = import nixpkgs { system = "aarch64-darwin"; };
        modules = [
          corioders-runner.homeManagerModules.default
          {
            home.username = "runner";
            home.homeDirectory = "/Users/runner";
            home.stateVersion = "26.05";
            services.coriodersExternalWorker = {
              enable = true;
              name = "alice-m1";
              capacity = 2;
              schedulerPublicKey = builtins.readFile ./scheduler.pub;
              scopes.corioders.tokenFile = "/Users/runner/.config/corioders/github.token";
            };
          }
        ];
      };
  };
}
```

Activate it with:

```bash
home-manager switch --flake .#runner@alice-m1
```

The module owns the complete service configuration. It installs a systemd user
service on Linux or a launchd agent on Apple Silicon Darwin, manages the
restricted scheduler key in `authorized_keys`, and starts one native JIT runner
per active lease. The operating system must already provide an SSH server.

Installing only the package exposes the protocol commands but does not enroll
or start a worker:

```bash
nix profile install github:corioders/nix-runner-worker
```

The scheduler core is also exported separately as
`packages.<system>.corioders-runner-scheduler` for trusted scheduler hosts.

Enrollment and priority remain central. The scheduler administrator adds the
host to their target list and assigns its priority; the worker cannot advertise
or change it.

## Trust boundary

The worker account requires a GitHub API token capable of creating JIT runners
for its configured scopes. Keep the token file readable only by that account.
The machine owner can inspect workflow code and data, so enroll only trusted
machines and do not route secrets to untrusted workers.

## Verification

```bash
nix flake check --all-systems --no-build
nix flake check
```

{
  description = "Reusable Nix-managed GitHub Actions worker targets for Corioders";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-26.05";
    nixpkgs-darwin.url = "github:NixOS/nixpkgs/nixpkgs-26.05-darwin";
    home-manager = {
      url = "github:nix-community/home-manager/release-26.05";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs =
    {
      home-manager,
      nixpkgs,
      nixpkgs-darwin,
      ...
    }:
    let
      systems = [
        "aarch64-darwin"
        "aarch64-linux"
        "x86_64-linux"
      ];
      forAllSystems = nixpkgs.lib.genAttrs systems;
      pkgsFor =
        system: import (if system == "aarch64-darwin" then nixpkgs-darwin else nixpkgs) { inherit system; };
      moduleCheck =
        system:
        let
          pkgs = pkgsFor system;
          homeDirectory = if pkgs.stdenv.hostPlatform.isDarwin then "/Users/runner" else "/home/runner";
          fakeRunner = pkgs.runCommand "fake-github-runner" { } ''
            mkdir -p "$out/bin" "$out/externals"
          '';
        in
        (home-manager.lib.homeManagerConfiguration {
          inherit pkgs;
          modules = [
            ./module.nix
            {
              home = {
                inherit homeDirectory;
                username = "runner";
                stateVersion = "26.05";
              };
              services.coriodersExternalWorker = {
                enable = true;
                name = "test-worker";
                runnerPackage = fakeRunner;
                schedulerPublicKey = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITest corioders-runner-scheduler";
                scopes.corioders.tokenFile = "/run/secrets/github-token";
              };
            }
          ];
        }).activationPackage;
    in
    {
      homeManagerModules = {
        default = import ./module.nix;
        corioders-runner-worker = import ./module.nix;
      };

      packages = forAllSystems (
        system:
        let
          pkgs = pkgsFor system;
        in
        {
          default = pkgs.callPackage ./worker/package.nix { };
          corioders-runner-worker = pkgs.callPackage ./worker/package.nix { };
        }
      );

      checks = forAllSystems (
        system:
        let
          pkgs = pkgsFor system;
        in
        {
          module = moduleCheck system;
          package = pkgs.callPackage ./worker/package.nix { };
          python =
            pkgs.runCommand "corioders-runner-python-tests" { nativeBuildInputs = [ pkgs.python3 ]; }
              ''
                python -m unittest discover -s ${./scheduler}
                touch "$out"
              '';
          quality =
            pkgs.runCommand "corioders-runner-quality"
              {
                nativeBuildInputs = [
                  pkgs.deadnix
                  pkgs.nixfmt
                  pkgs.statix
                ];
              }
              ''
                find ${./.} -type f -name '*.nix' -exec nixfmt --check {} +
                deadnix --fail ${./.}
                statix check ${./.}
                touch "$out"
              '';
        }
      );
    };
}

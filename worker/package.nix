{
  lib,
  symlinkJoin,
  writeShellApplication,
  scheduler ? callPackage ../scheduler/package.nix { },
  callPackage,
}:
let
  target = writeShellApplication {
    name = "corioders-runner-target";
    runtimeInputs = [ scheduler ];
    text = ''
      exec corioders-runner-scheduler serve-ssh
    '';
  };
  pool = writeShellApplication {
    name = "corioders-runner-worker-pool";
    runtimeInputs = [ scheduler ];
    text = ''
      exec corioders-runner-scheduler worker-pool "$@"
    '';
  };
in
symlinkJoin {
  name = "corioders-runner-worker";
  paths = [
    scheduler
    target
    pool
  ];
  meta = {
    description = "Corioders external GitHub Actions worker target and pool";
    license = lib.licenses.mit;
    platforms = [
      "aarch64-darwin"
      "aarch64-linux"
      "x86_64-linux"
    ];
  };
}

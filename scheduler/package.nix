{
  lib,
  python3,
  writeShellApplication,
}:

let
  scheduler = ./scheduler.py;
in
writeShellApplication {
  name = "corioders-runner-scheduler";
  runtimeInputs = [ python3 ];
  text = ''
    exec python3 ${scheduler} "$@"
  '';
  meta = {
    description = "Select and reserve corioders GitHub Actions runner targets";
    license = lib.licenses.mit;
    mainProgram = "corioders-runner-scheduler";
    platforms = lib.platforms.unix;
  };
}

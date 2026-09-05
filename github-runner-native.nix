{ lib, pkgs }:
let
  organization = "corioders";
in
{
  mkRunnerService =
    {
      extraRuntimeInputs ? [ ],
      jobStartedHook ? null,
      labels,
      namePrefix,
      repository ? null,
      runnerGroupName ? "Default",
      runnerPackage,
      stateDirectory,
      tokenFile,
    }:
    let
      runnerApiBase =
        if repository == null then
          "https://api.github.com/orgs/${organization}/actions/runners"
        else
          "https://api.github.com/repos/${repository}/actions/runners";
    in
    pkgs.writeShellApplication {
      name = "github-runner-${namePrefix}";
      runtimeInputs = [
        pkgs.coreutils
        pkgs.curl
        pkgs.jq
      ]
      ++ extraRuntimeInputs;
      text = ''
        runner_id=""
        authorization_header=""
        api_version_header="X-GitHub-Api-Version: 2026-03-10"

        cleanup() {
          if [[ -n "$runner_id" && -n "$authorization_header" ]]; then
            curl --fail --silent --show-error \
              --request DELETE \
              --header "Accept: application/vnd.github+json" \
              --header "$authorization_header" \
              --header "$api_version_header" \
              ${lib.escapeShellArg runnerApiBase}/"$runner_id" \
              >/dev/null 2>&1 || true
          fi
        }
        trap cleanup EXIT INT TERM

        if [[ ! -s ${lib.escapeShellArg tokenFile} ]]; then
          echo "GitHub runner API token is missing: ${tokenFile}" >&2
          exit 78
        fi

        api_token="$(<${lib.escapeShellArg tokenFile})"
        authorization_header="Authorization: Bearer $api_token"

        ${lib.optionalString (repository == null) ''
          runner_group_id="$(
            curl --fail --silent --show-error \
              --retry 5 \
              --retry-all-errors \
              --retry-delay 1 \
              --retry-max-time 30 \
              --header "Accept: application/vnd.github+json" \
              --header "$authorization_header" \
              --header "$api_version_header" \
              https://api.github.com/orgs/${organization}/actions/runner-groups \
              | jq --exit-status --raw-output \
                ${lib.escapeShellArg ".runner_groups[] | select(.name == \"${runnerGroupName}\") | .id"}
          )"
        ''}
        ${lib.optionalString (repository != null) ''
          runner_group_id=1
        ''}

        runner_name=${lib.escapeShellArg namePrefix}-"$(date -u +%Y%m%d-%H%M%S)"-"$$"
        jit_response="$(
          jq --compact-output --null-input \
            --arg name "$runner_name" \
            --argjson labels ${lib.escapeShellArg (builtins.toJSON labels)} \
            --argjson runner_group_id "$runner_group_id" \
            '{
              name: $name,
              runner_group_id: $runner_group_id,
              labels: $labels,
              work_folder: "_work"
            }' \
          | curl --fail --silent --show-error \
              --retry 5 \
              --retry-all-errors \
              --retry-delay 1 \
              --retry-max-time 30 \
              --request POST \
              --header "Accept: application/vnd.github+json" \
              --header "Content-Type: application/json" \
              --header "$authorization_header" \
              --header "$api_version_header" \
              --data @- \
              ${lib.escapeShellArg runnerApiBase}/generate-jitconfig
        )"
        runner_id="$(jq --exit-status --raw-output .runner.id <<<"$jit_response")"
        jit_config="$(jq --exit-status --raw-output .encoded_jit_config <<<"$jit_response")"
        unset api_token jit_response

        runner_state_directory="''${CORIODERS_RUNNER_INSTANCE_STATE_DIRECTORY:-${lib.escapeShellArg stateDirectory}}"
        mkdir -p "$runner_state_directory"
        cd "$runner_state_directory"
        runner_root="$runner_state_directory/runner"
        runner_version=${lib.escapeShellArg (toString runnerPackage)}
        if [[ ! -r "$runner_root/.wjsetup-version" ]] \
          || [[ "$(<"$runner_root/.wjsetup-version")" != "$runner_version" ]]; then
          temporary_root="$runner_root.tmp.$$"
          rm -rf "$temporary_root"
          mkdir -p "$temporary_root"
          cp -R ${runnerPackage}/bin "$temporary_root/bin"
          chmod -R u+w "$temporary_root/bin"
          ln -s ${runnerPackage}/externals "$temporary_root/externals"
          printf '%s\n' "$runner_version" >"$temporary_root/.wjsetup-version"
          rm -rf "$runner_root"
          mv "$temporary_root" "$runner_root"
        fi
        ${lib.optionalString (jobStartedHook != null) ''
          export ACTIONS_RUNNER_HOOK_JOB_STARTED=${lib.escapeShellArg jobStartedHook}
        ''}
        "$runner_root/bin/Runner.Listener" run --jitconfig "$jit_config"
      '';
    };
}

#!/bin/bash
set -euo pipefail

exec /usr/local/bin/corioders-runner-scheduler validate-event \
  --event-name "${GITHUB_EVENT_NAME:?}" \
  --event-path "${GITHUB_EVENT_PATH:?}"

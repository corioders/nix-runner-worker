#!/bin/sh
set -eu

case "${GITHUB_EVENT_NAME:?}" in
  pull_request | pull_request_target) ;;
  *) exit 0 ;;
esac

node - "${GITHUB_EVENT_PATH:?}" <<'EOF'
const fs = require("node:fs");

const event = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
const head = event.pull_request?.head?.repo;
const base = event.pull_request?.base?.repo;
if (head?.fork || !head?.full_name || head.full_name !== base?.full_name) {
  console.error("fork pull requests may not use corioders self-hosted runners");
  process.exit(1);
}
EOF

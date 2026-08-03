#!/usr/bin/env bash
set -euo pipefail

if [[ "${AGENTFLOW_INTEGRATION:-}" != "1" ]]; then
  echo "Refusing to run real integration checks without AGENTFLOW_INTEGRATION=1." >&2
  exit 2
fi

command -v bd >/dev/null
command -v agentflow >/dev/null
bd --version
agentflow --version

integration_root="$(mktemp -d)"
trap 'rm -rf -- "${integration_root}"' EXIT
project="${integration_root}/project"
mkdir "${project}"
git -C "${project}" init --quiet
agentflow init "${project}" --beads
agentflow beads status "${project}"

test -f "${project}/.agentflow/config.json"
test -d "${project}/.beads"
echo "Real Agentflow + Beads initialization passed."

#!/usr/bin/env bash
set -euo pipefail

if [[ "${AGENTFLOW_INTEGRATION:-}" != "1" ]]; then
  echo "Refusing to run real integration checks without AGENTFLOW_INTEGRATION=1." >&2
  exit 2
fi

command -v bd >/dev/null
command -v agentflow >/dev/null
beads_version="$(bd --version)"
printf '%s\n' "${beads_version}"
if [[ "${beads_version}" != *"1.1.0"* ]]; then
  echo "Expected Beads 1.1.0 for this integration smoke." >&2
  exit 1
fi
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

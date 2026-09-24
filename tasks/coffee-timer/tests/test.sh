#!/usr/bin/env bash
set -eu
exec uv run --no-project --python 3.12 \
  --with 'harbor-rewardkit[jev] @ git+https://github.com/harbor-framework/harbor.git@404bae708cf284f4c6ed054422ad4f5b2aca764f#subdirectory=packages/rewardkit' \
  python - <<'PY'
from pathlib import Path
from rewardkit import format_trajectory, run

# JEV accepts text files, not RewardKit's atif-trajectory option.
Path("/logs/verifier/trajectory.txt").write_text(
    format_trajectory("/logs/agent/trajectory.json", max_tokens=16000)
)
run("/tests")
PY

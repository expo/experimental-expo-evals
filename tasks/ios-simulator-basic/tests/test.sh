#!/usr/bin/env bash
set -eu
exec uv run --no-project --python 3.12 \
  --with 'harbor-rewardkit[jev] @ git+https://github.com/harbor-framework/harbor.git@404bae708cf284f4c6ed054422ad4f5b2aca764f#subdirectory=packages/rewardkit' \
  python - <<'PY'
import json
from pathlib import Path
from rewardkit import format_trajectory, run

Path("/logs/verifier/trajectory.txt").write_text(
    format_trajectory("/logs/agent/trajectory.json", max_tokens=16000)
)
screenshot = Path("/app/evidence/screenshot.png")
evidence = {"path": str(screenshot), "exists": False, "bytes": 0, "png_signature": False}
try:
    with screenshot.open("rb") as file:
        signature = file.read(8)
    evidence.update(exists=True, bytes=screenshot.stat().st_size,
                    png_signature=signature == b"\x89PNG\r\n\x1a\n")
except OSError:
    pass
Path("/logs/verifier/screenshot.json").write_text(json.dumps(evidence))
run("/tests")
PY

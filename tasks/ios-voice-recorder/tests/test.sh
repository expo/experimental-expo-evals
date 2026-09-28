#!/usr/bin/env bash
set -euo pipefail
exec uv run --no-project --python 3.12 \
  --with 'harbor-rewardkit[jev] @ git+https://github.com/harbor-framework/harbor.git@404bae708cf284f4c6ed054422ad4f5b2aca764f#subdirectory=packages/rewardkit' \
  python /tests/verify.py

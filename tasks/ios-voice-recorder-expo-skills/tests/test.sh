#!/usr/bin/env bash
set -euo pipefail
# Install the verifier's UI-test tool after the agent has finished.
curl --fail --location --silent --show-error \
  https://github.com/mobile-dev-inc/Maestro/releases/download/cli-2.10.0/maestro.zip \
  --output /tmp/maestro.zip
printf '29b675e10cc12080e445e9bfb2e2b4e4dfb9c0f2e30d5884120d258b5e1cd991  /tmp/maestro.zip\n' | shasum -a 256 -c -
unzip -q -o /tmp/maestro.zip -d /tmp/harbor-maestro
export PATH="/tmp/harbor-maestro/maestro/bin:$PATH"

exec uv run --no-project --python 3.12 \
  --with 'harbor-rewardkit[jev] @ git+https://github.com/harbor-framework/harbor.git@404bae708cf284f4c6ed054422ad4f5b2aca764f#subdirectory=packages/rewardkit' \
  python /tests/verify.py

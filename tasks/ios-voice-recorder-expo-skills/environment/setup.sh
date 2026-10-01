#!/usr/bin/env bash
set -euo pipefail

npx --yes @swmansion/argent@latest init --yes --global --no-telemetry
npx --yes skills@latest add https://github.com/expo/skills/tree/efa52f0a9d2176db75992736281c77da1b714fa3 --skill '*' --agent claude-code codex --yes

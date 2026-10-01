#!/usr/bin/env bash
set -euo pipefail
npm install --yes --global eas-cli
npx --yes skills@latest add https://github.com/expo/skills/tree/efa52f0a9d2176db75992736281c77da1b714fa3 --skill '*' --agent claude-code codex --yes

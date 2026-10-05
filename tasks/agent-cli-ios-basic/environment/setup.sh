#!/usr/bin/env bash
set -euo pipefail

bunx create-expo-app@5.0.0 /app/expo-app --template default@sdk-57 --yes
cd /app/expo-app
bunx @expo/agent-cli agents:setup --yes --agent claude-code --agent codex --no-plugins
npx --yes @swmansion/argent@latest init --yes --global --no-telemetry

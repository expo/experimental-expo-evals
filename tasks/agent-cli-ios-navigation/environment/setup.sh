#!/usr/bin/env bash
set -euo pipefail

task_environment="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
bunx create-expo-app@5.0.0 /app/expo-app --template default@sdk-57 --yes
cd /app/expo-app
rm -rf src
cp -R "$task_environment/app/." .
bunx @expo/agent-cli agents:setup --yes --agent claude-code --agent codex --no-plugins
npx --yes @swmansion/argent@latest init --yes --global --no-telemetry

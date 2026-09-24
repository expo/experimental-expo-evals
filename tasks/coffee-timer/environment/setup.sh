#!/usr/bin/env bash
set -eu
npm install --yes --global eas-cli
npx --yes skills@latest add expo/skills --skill '*' --agent claude-code codex --yes

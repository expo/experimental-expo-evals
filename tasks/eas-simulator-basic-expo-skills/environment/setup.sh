#!/usr/bin/env bash
set -euo pipefail
npm install --yes --global eas-cli

skills_commit=efa52f0a9d2176db75992736281c77da1b714fa3
skills_archive=$(mktemp)
trap 'rm -f "$skills_archive"' EXIT
curl --fail --location --silent --show-error \
  "https://github.com/expo/skills/archive/${skills_commit}.tar.gz" \
  --output "$skills_archive"
mkdir -p /skills
tar -xzf "$skills_archive" --strip-components=4 -C /skills \
  "skills-${skills_commit}/plugins/expo/skills"

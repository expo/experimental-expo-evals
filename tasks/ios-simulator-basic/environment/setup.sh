#!/usr/bin/env bash
set -euo pipefail
if [ "$(uname -s)" != Darwin ]; then
  echo 'This task requires the EAS Workflows macOS environment.' >&2
  exit 1
fi
xcodebuild -version

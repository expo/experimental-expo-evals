#!/usr/bin/env bash
set -euo pipefail

if [ "$(uname -s)" != Darwin ]; then
  echo 'The EAS Workflows environment requires a macOS worker.' >&2
  exit 1
fi
if [ "${EAS_BUILD:-}" != true ]; then
  echo 'Run this bootstrap only inside a disposable EAS workflow machine.' >&2
  exit 1
fi

bundle_dir="$(cd "$(dirname "$0")" && pwd)"
sandbox_root=/private/tmp/harbor
deadline=$((SECONDS + 3300))
mkdir -p "$sandbox_root"

# Real root aliases keep native Harbor commands and task scripts unchanged.
# macOS exposes these through APFS because its root filesystem is read-only.
manifest="$sandbox_root/synthetic.conf"
: > "$manifest"
for name in app logs tests solution installed-agent harbor; do
  mkdir -p "$sandbox_root/$name"
  printf '%s\tprivate/tmp/harbor/%s\n' "$name" "$name" >> "$manifest"
done
sudo -n mkdir -p /etc/synthetic.d
sudo -n install -m 644 "$manifest" /etc/synthetic.d/harbor
sudo -n /System/Library/Filesystems/apfs.fs/Contents/Resources/apfs.util -t || true
for name in app logs tests solution installed-agent harbor; do
  if [ "$(cd "/$name" 2>/dev/null && pwd -P)" != "$sandbox_root/$name" ]; then
    echo "macOS did not create the required Harbor path /$name." >&2
    exit 1
  fi
done
mkdir -p /logs/agent /logs/verifier /logs/artifacts

export PATH="$HOME/.local/bin:$PATH"
curl -LsSf https://astral.sh/uv/0.12.18/install.sh | sh
uv python install 3.12
ln -s "$(uv python find 3.12)" /private/tmp/harbor-python
cp "$bundle_dir/harbor_rpc.py" /private/tmp/harbor-rpc.py

# Image setup does not inherit credentials provided by the EAS control plane.
cd /app
env -i HOME="$HOME" USER="$(id -un)" LOGNAME="$(id -un)" \
  PATH="$PATH" SHELL=/bin/bash TMPDIR="${TMPDIR:-/private/tmp}" LANG=en_US.UTF-8 \
  bash "$bundle_dir/environment/setup.sh"
printf '%s' "$PATH" > /private/tmp/harbor-path
touch /private/tmp/harbor-ready

while [ "$SECONDS" -lt "$deadline" ]; do
  sleep 2
done
echo 'The Harbor sandbox reached its 55-minute lifetime.' >&2
exit 1

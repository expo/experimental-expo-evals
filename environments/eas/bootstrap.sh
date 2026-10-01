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
mkdir -p "$sandbox_root"

# Real root aliases keep native Harbor commands and task scripts unchanged.
# macOS exposes these through APFS because its root filesystem is read-only.
manifest="$sandbox_root/synthetic.conf"
: > "$manifest"
for name in app logs tests solution installed-agent harbor skills; do
  mkdir -p "$sandbox_root/$name"
  printf '%s\tprivate/tmp/harbor/%s\n' "$name" "$name" >> "$manifest"
done
sudo -n mkdir -p /etc/synthetic.d
sudo -n install -m 644 "$manifest" /etc/synthetic.d/harbor
sudo -n /System/Library/Filesystems/apfs.fs/Contents/Resources/apfs.util -t || true
for name in app logs tests solution installed-agent harbor skills; do
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
# Keep connection credentials and helper state outside ordinary agent files.
# EAS grants the VM user sudo; the credential is scoped to this disposable trial.
bridge_dir=/private/tmp/harbor-bridge
sudo -n mkdir -m 700 "$bridge_dir"
sudo -n install -m 600 "$bundle_dir/bridge.json" "$bridge_dir/config.json"
sudo -n install -m 600 "$bundle_dir/harbor_rpc.py" "$bridge_dir/rpc.py"
sudo -n install -m 600 "$bundle_dir/harbor_worker.py" "$bridge_dir/worker.py"
rm "$bundle_dir/bridge.json"

# Image setup does not inherit credentials provided by the EAS control plane.
cd /app
env -i HOME="$HOME" USER="$(id -un)" LOGNAME="$(id -un)" \
  PATH="$PATH" SHELL=/bin/bash TMPDIR="${TMPDIR:-/private/tmp}" LANG=en_US.UTF-8 \
  bash "$bundle_dir/environment/setup.sh"
printf '%s' "$PATH" > /private/tmp/harbor-path
touch /private/tmp/harbor-ready

# This foreground workflow step owns the worker lifetime, independent of HTTP requests.
exec sudo -n /private/tmp/harbor-python "$bridge_dir/worker.py" "$bridge_dir/config.json"

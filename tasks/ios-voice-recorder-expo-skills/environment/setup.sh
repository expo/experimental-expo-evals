#!/usr/bin/env bash
set -euo pipefail

if [ "$(uname -s)" != Darwin ]; then
  echo 'This task requires the EAS Workflows macOS environment.' >&2
  exit 1
fi
xcodebuild -version
java -version

# A prebuilt, pinned CLI avoids a Homebrew build during each trial.
maestro_archive=$(mktemp)
trap 'rm -f "$maestro_archive"' EXIT
curl --fail --location --silent --show-error \
  https://github.com/mobile-dev-inc/Maestro/releases/download/cli-2.10.0/maestro.zip \
  --output "$maestro_archive"
printf '29b675e10cc12080e445e9bfb2e2b4e4dfb9c0f2e30d5884120d258b5e1cd991  %s\n' "$maestro_archive" | shasum -a 256 -c -
mkdir -p /private/tmp/harbor-maestro
unzip -q "$maestro_archive" -d /private/tmp/harbor-maestro
sudo -n ln -sf /private/tmp/harbor-maestro/maestro/bin/maestro /usr/local/bin/maestro
maestro --version

simulator_id=$(xcrun simctl list devices available --json | node -e '
let input = "";
process.stdin.on("data", chunk => input += chunk);
process.stdin.on("end", () => {
  const iphones = Object.values(JSON.parse(input).devices).flat()
    .filter(device => device.isAvailable && device.name.startsWith("iPhone"));
  const device = iphones.find(device => device.state === "Booted") || iphones[0];
  if (!device) throw new Error("No available iPhone simulator on this macOS image");
  process.stdout.write(device.udid);
});')
if ! xcrun simctl list devices booted | grep -q "$simulator_id"; then
  xcrun simctl boot "$simulator_id"
fi
xcrun simctl bootstatus "$simulator_id" -b
printf '%s\n' "$simulator_id" > /app/simulator-udid.txt
mkdir -p /app/evidence

# Harbor installs this pinned skill tree for the selected agent harness.
skills_commit=efa52f0a9d2176db75992736281c77da1b714fa3
skills_archive=$(mktemp)
trap 'rm -f "$maestro_archive" "$skills_archive"' EXIT
curl --fail --location --silent --show-error \
  "https://github.com/expo/skills/archive/${skills_commit}.tar.gz" \
  --output "$skills_archive"
mkdir -p /skills
tar -xzf "$skills_archive" --strip-components=4 -C /skills \
  "skills-${skills_commit}/plugins/expo/skills"

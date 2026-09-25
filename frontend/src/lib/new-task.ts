import type { ConfigFile } from "./types";

const files: Record<string, string> = {
  "instruction.md": "",
  "environment/setup.sh": "#!/usr/bin/env bash\nset -eu\n",
  "tests/rubric.toml": `[judge]
judge = "jev"
files = ["/logs/verifier/trajectory.txt"]

[[criterion]]
name = "task_completed"
description = "The recorded actions and tool results demonstrate that the agent completed the task in its instructions. Unsupported claims in the final response are not sufficient evidence."
type = "binary"
`,
  "task.toml": `schema_version = "1.4"

[verifier.env]
TYPESAFE_API_KEY = "\${TYPESAFE_API_KEY}"
`,
  "environment/Dockerfile": `FROM node:24-bookworm

RUN curl -LsSf https://astral.sh/uv/0.12.18/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh
RUN uv python install 3.12

WORKDIR /app
COPY setup.sh /setup.sh
RUN bash /setup.sh
`,
  "tests/test.sh": `#!/usr/bin/env bash
set -eu
exec uv run --no-project --python 3.12 \\
  --with 'harbor-rewardkit[jev] @ git+https://github.com/harbor-framework/harbor.git@404bae708cf284f4c6ed054422ad4f5b2aca764f#subdirectory=packages/rewardkit' \\
  python - <<'PY'
from pathlib import Path
from rewardkit import format_trajectory, run

Path("/logs/verifier/trajectory.txt").write_text(
    format_trajectory("/logs/agent/trajectory.json", max_tokens=16000)
)
run("/tests")
PY
`,
};

export function createTaskFiles(id = "new-task"): ConfigFile[] {
  return Object.entries(files).map(([path, content]) => ({
    path: `tasks/${id}/${path}`,
    content,
  }));
}

// Generated from native task files by npm run seed:sync. No credentials.
export const seedTask = {
  "id": "coffee-timer",
  "name": "Coffee timer",
  "files": [
    {
      "path": "tasks/coffee-timer/instruction.md",
      "content": "Create a new expo app with sdk 57 - a coffee timer, run it on EAS simulator (use eas simulator --help for more info) and download a screenshot.\n"
    },
    {
      "path": "tasks/coffee-timer/tests/rubric.toml",
      "content": "[judge]\njudge = \"jev\"\nfiles = [\"/logs/verifier/trajectory.txt\"]\n\n[[criterion]]\nname = \"expo_sdk_57\"\ndescription = \"The recorded file changes and tool results show that the agent created a new Expo app using SDK 57. A statement in its final answer alone is not sufficient evidence.\"\ntype = \"binary\"\n\n[[criterion]]\nname = \"coffee_timer\"\ndescription = \"The recorded implementation shows a coffee timer with a countdown and controls to operate it. Judge the implementation evidence, not just the agent's claim that it finished.\"\ntype = \"binary\"\n\n[[criterion]]\nname = \"eas_simulator\"\ndescription = \"Tool results show that the agent successfully ran this app on EAS Simulator. Merely reading help, starting an empty simulator, or attempting a failed launch does not pass.\"\ntype = \"binary\"\n\n[[criterion]]\nname = \"downloaded_screenshot\"\ndescription = \"Tool results show a screenshot of the running app was successfully downloaded to a local file. A remote URL or an unsupported claim alone does not pass.\"\ntype = \"binary\"\n\n[scoring]\naggregation = \"all-pass\"\n"
    },
    {
      "path": "tasks/coffee-timer/task.toml",
      "content": "schema_version = \"1.4\"\nartifacts = [\n  { source = \"/app\", destination = \"app\", exclude = [\"**/node_modules/**\", \"**/.git/**\", \"**/.env*\", \"**/.expo/**\", \"**/.npm/**\", \"**/*.log\"] },\n]\n\n[agent]\ntimeout_sec = 1800\n\n[verifier]\ntimeout_sec = 300\n\n[verifier.env]\nTYPESAFE_API_KEY = \"${TYPESAFE_API_KEY}\"\n\n[environment]\nbuild_timeout_sec = 900\ncpus = 2\nmemory_mb = 4096\n\n[environment.env]\nEXPO_TOKEN = \"${EXPO_TOKEN}\"\n"
    },
    {
      "path": "tasks/coffee-timer/environment/Dockerfile",
      "content": "FROM node:24-bookworm\n\nRUN curl -LsSf https://astral.sh/uv/0.12.18/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh\nRUN uv python install 3.12\n\nWORKDIR /app\nCOPY setup.sh /setup.sh\nRUN bash /setup.sh\n"
    },
    {
      "path": "tasks/coffee-timer/environment/setup.sh",
      "content": "#!/usr/bin/env bash\nset -eu\nnpm install --yes --global eas-cli\nnpx --yes skills@latest add expo/skills --skill '*' --agent claude-code codex --yes\n"
    },
    {
      "path": "tasks/coffee-timer/tests/test.sh",
      "content": "#!/usr/bin/env bash\nset -eu\nexec uv run --no-project --python 3.12 \\\n  --with 'harbor-rewardkit[jev] @ git+https://github.com/harbor-framework/harbor.git@404bae708cf284f4c6ed054422ad4f5b2aca764f#subdirectory=packages/rewardkit' \\\n  python - <<'PY'\nfrom pathlib import Path\nfrom rewardkit import format_trajectory, run\n\n# JEV accepts text files, not RewardKit's atif-trajectory option.\nPath(\"/logs/verifier/trajectory.txt\").write_text(\n    format_trajectory(\"/logs/agent/trajectory.json\", max_tokens=16000)\n)\nrun(\"/tests\")\nPY\n"
    }
  ]
};

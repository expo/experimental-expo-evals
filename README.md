# Harbor basics: coffee timer

One task, Claude Code with Opus 5.5, and a JEV judge using RewardKit.
Choose an E2B Linux sandbox or an EAS Workflows macOS machine.
The original task prompt is in `tasks/coffee-timer/instruction.md`.

## Shared web app

The `frontend/` app uses TanStack Start on Cloudflare Workers, D1 for drafts and
revision history, GitHub for committed native task files, and R2 for results.
A Cloudflare Container launches Harbor's Python CLI and controls the selected
remote sandbox. A regular Worker cannot launch the Harbor CLI as a subprocess.

```sh
uv sync --locked
cd frontend
npm ci
npm run dev:secrets
npm run db:migrate
npm run dev
```

Open **http://127.0.0.1:3000**. Local development runs real Workers bindings with
D1 and R2 emulated on disk under ignored `.wrangler/`. `dev:secrets` copies only
known API credentials from the root `.env` into ignored `.dev.vars`, without
printing them. Production uses Wrangler secrets and a shared username/password
login. Cloudflare Access is an optional upgrade.

The UI has two views: Tasks and Runs. Select a task file to edit its exact contents,
including `instruction.md`, `environment/setup.sh`, and `tests/rubric.toml`.
New tasks start with a blank prompt, an empty setup script, and one generic JEV
criterion. They do not copy the coffee-timer task. The environment Dockerfile,
`task.toml`, and RewardKit `tests/test.sh` are available in the same file selector.
The setup editor has a sandbox selector. EAS Workflows adds
`environment/workflow.yml`; both providers run the same `environment/setup.sh`.
The selection lives in `[metadata].sandbox_provider` in `task.toml`. Older tasks
without this setting use E2B. Saving and committing include this choice.

- **Suggest an edit** returns a proposal using the configured Anthropic model.
  Review it, then Apply or Discard. Applying only changes the browser draft.
- **Save draft** writes an atomic D1 revision shared with other users. It does
  not create a Git commit or write into this machine's `tasks/` directory.
- **Commit changes** writes the native files to the configured GitHub branch.
  A GitHub App supplies the bot identity with access to this repository only.
  Independent file changes merge. Conflicting files show draft and Git versions
  and require an explicit choice or edited resolution. No force push is used.
- **Run** requires the saved revision to be committed. The runner receives
  an immutable copy of those files and their commit SHA. Agent/model choices
  apply to the run. Closing the browser does not stop the container.

If a shared draft changes while you edit, stale saves are rejected. Copy any
browser edits you want to keep before loading the latest version.
The first version allows one active eval at a time. Other users wait until it
finishes. It has no queued backlog or automatic paid retries.

Results include the task snapshot, run configuration, scores, trace, logs, and
artifacts. Existing local `jobs/` remain available with `uv run harbor view jobs`;
they are not automatically uploaded or imported into the shared database.
The native CLI below still works independently of the web app. To update the
initial bundled example after changing its local files, use `npm run seed:sync`.
This does not overwrite existing D1 drafts.

See [Cloudflare deployment](DEPLOYMENT.md) for provisioning, authentication,
secrets, and Wrangler commands. Production requires a configured login.
Local container execution is disabled by default so editing and
AI assistance do not require Docker.

```sh
npm run check
npm test
npm run build
```

Tests use SQLite and mocked GitHub/model calls. Runner tests are available with
`uv run python -m unittest discover -s runner -p 'test_*.py'` from the repository root.

## What you write

```text
harbor.yaml                       Which agent/model/task to run
e2b_environment.py                One-hour E2B timeout compatibility adapter
eas_environment.py                Harbor adapter for EAS Workflows over SSH
eas_provider/                     macOS bootstrap and default workflow
tasks/coffee-timer/
  instruction.md                  The prompt
  task.toml                       Time limits, credentials, saved artifacts
  environment/
    Dockerfile                    E2B Node + verifier runtime; runs setup.sh
    workflow.yml                  EAS macOS image and bootstrap step
    setup.sh                      Installs EAS CLI and Expo skills
  tests/
    test.sh                       Formats the trace and starts RewardKit
    rubric.toml                   Plain-language grading criteria
```

`pyproject.toml` and `uv.lock` install Harbor, RewardKit, and the environment adapters.
There is no custom eval command or grading implementation.

## What the frameworks provide

| Harbor | RewardKit |
| --- | --- |
| Creates the sandbox and installs the selected agent | Reads the rubric and recorded agent trace |
| Runs the prompt through Claude Code or Codex | Calls the judge model |
| Makes `/logs/agent/trajectory.json` available to the verifier | Scores each criterion and combines the scores |
| Runs `tests/test.sh`, saves results/artifacts, removes the sandbox | Writes `reward.json` and `reward-details.json` |
| Provides the CLI and results frontend | Saves JEV's raw probabilities and criterion scores |

EAS Simulator is a separate service the evaluated agent uses. Harbor does not
provide an Expo-specific grader or manage EAS sessions. The installed Expo
skills explain how to use the service and clean up sessions.

## Environment setup

Harbor uses the Dockerfile as the E2B template recipe. E2B builds it remotely;
this does not require Docker on your Mac. Node's base image already includes
git, curl, and CA certificates. The extra uv/Python installation is for RewardKit.

`environment/setup.sh` contains the task's tool setup:

```sh
npm install --yes --global eas-cli
npx --yes skills@latest add expo/skills --skill '*' --agent claude-code codex --yes
```

The explicit agent list installs project skills for both harnesses before their
CLIs exist. The Dockerfile runs this from `/app`, creating `.agents/skills` and
`.claude/skills`. Harbor installs the selected agent itself. Agent-device can
be run through npx when the Expo skills call for it.

Harbor 0.23 has no standalone setup-script setting for a normal single-step task,
so the small Dockerfile invokes the script. A prebuilt image could replace it,
but would need to be built and published separately. These commands use current
EAS CLI and Expo skills at image build time; E2B can reuse the resulting template.

### EAS Workflows

The hosted Harbor process uses `eas_environment:EASWorkflowEnvironment`, a
Harbor `BaseEnvironment` implementation. It uploads only the task environment
and adapter bootstrap into a dedicated EAS project, starts one macOS workflow
with SSH enabled, and uses SSH for commands and file transfers. Harbor still
installs the agent, collects its trajectory, runs RewardKit, and saves results.
The adapter ends the workflow when Harbor finishes. Its bootstrap also has a
55-minute deadline for its wait loop if the controller disappears.

macOS uses a workflow image, not a Dockerfile. The default
`environment/workflow.yml` selects a macOS machine and Node 24. The adapter's
bootstrap installs uv/Python, prepares Harbor's paths, and runs `setup.sh` in
`/app`. The Dockerfile remains the E2B recipe. This provider uses EAS CLI's
experimental workflow SSH support, pinned to CLI 24.7.0 in the hosted runner.
On September 25, 2026, the live macOS bootstrap passed, but EAS rejected SSH
jobs with "no SSH relay is configured." Workflow SSH must be enabled by EAS
before this provider can run an eval end to end. A subsequent source review also
found that the relay requires a PTY and forces a login shell; the current
non-interactive command transport needs adaptation and validation. Treat this
provider as a prototype. E2B remains the working default.

Configure `EXPO_TOKEN` and `EAS_PROJECT_ID` for the dedicated workflow project.
Local EAS runs also need Node 24, `npm install -g eas-cli@24.7.0`, and OpenSSH.
The dashboard selects the adapter from the task metadata. With the native Harbor
CLI, select it explicitly in `harbor.yaml`:

```yaml
environment:
  import_path: eas_environment:EASWorkflowEnvironment
```

EAS Workflows and EAS Simulator are separate services. The workflow hosts the
coding agent; the coffee-timer prompt still asks that agent to use EAS Simulator.
Workflow compute usage applies in addition to model, judge, and simulator usage.

## Run

Harbor creates an E2B Linux sandbox for the coding agent. EAS hosts the remote
mobile simulator. Local Docker is not needed.

```sh
cd ~/Developer/experiments/eas-simulator-evals
uv sync --locked
```

Your existing `.env` is preserved and ignored by git. On a fresh clone, copy
`.env.example` to `.env` and fill in the credentials. The task forwards
`EXPO_TOKEN` to the sandbox and `TYPESAFE_API_KEY` to the verifier. Harbor reads
`E2B_API_KEY` for sandbox access and `ANTHROPIC_API_KEY` for Claude Code.
Fill in the empty `TYPESAFE_API_KEY` entry before running an eval.

```sh
# Check configuration without starting an eval.
uv run harbor run --env-file .env -c harbor.yaml --dry-run

# Run one trial. This uses the agent API, JEV, E2B, and EAS Simulator.
uv run harbor run --env-file .env -c harbor.yaml

# Inspect results. Keep this command running while using the frontend.
uv run --env-file .env harbor view jobs
```

The coding agent creates files under `/app`; Harbor saves them under each
trial's `artifacts/app/`. Dependencies and credential files are excluded.
Results and traces live in `jobs/`, which is ignored by git.

## Change one thing

Edit `agents[0].model_name` in `harbor.yaml`, or override it with
`--model anthropic/MODEL_ID`. To run three fresh attempts sequentially:

```sh
uv run harbor run --env-file .env -c harbor.yaml --n-attempts 3 --n-concurrent 1
```

The coding model is configured in `harbor.yaml`. The judge model is configured
independently in `tests/rubric.toml`. Keep the judge fixed when comparing agents.

Later, add `OPENAI_API_KEY` to `.env` and use the same task through Codex:

```sh
uv run harbor run --env-file .env -c harbor.yaml --agent codex --model openai/gpt-5.4
```

The TypeSafe key is still needed because the judge remains JEV.

## Understand the score

The four binary criteria check for evidence of SDK 57, a coffee timer,
successful EAS execution, and a downloaded screenshot. `all-pass` gives an
overall `reward` of 1 only when every criterion passes. JEV's binary threshold
is 0.5. Inspect `reward-details.json` for individual scores and raw probabilities.
JEV does not return written reasoning.

This first judge reads the recorded conversation and tool results. It does
not inspect screenshot pixels or interact with the app. Its score is an LLM's
assessment of that evidence, not an independent test that the timer works.
JEV accepts text files and does not support RewardKit's `atif-trajectory` option.
`test.sh` uses RewardKit's public `format_trajectory()` to produce a text file,
retaining every step and shortening long content blocks with a 16k token target.
Formatting is local and does not call another model. JEV's input limit is 32k
tokens; unusually long traces can still exceed it and cause a verifier error.

JEV support is not in RewardKit's published 0.2.1 release yet. Both the local
dependency and verifier pin upstream commit
`404bae708cf284f4c6ed054422ad4f5b2aca764f`, which adds the JEV integration.

`tests/test.sh` is Harbor's standard verifier entry point, not a custom test
framework. It calls RewardKit's `format_trajectory()` and `run()` functions.
The formatting call is needed because JEV cannot use `atif-trajectory`, and the
raw trace from our previous run is too large for its context. The rubric owns
all grading rules; the script only connects the framework tools.

To validate rubric syntax without a model call:

```sh
uv run python -c 'import rewardkit; rewardkit.discover("tasks/coffee-timer/tests"); print("Rubric OK")'
```

Previous jobs were generated with the earlier custom verifier and retain those
scores. Start new runs from `harbor.yaml`; old job configurations refer to the
previous setup.

## E2B timeout compatibility

Harbor 0.23.0 requests a 24-hour E2B sandbox and exposes no timeout setting.
The current E2B Hobby plan permits one hour. `e2b_environment.py` changes only
that creation timeout to 3600 seconds; Harbor handles the remaining lifecycle.
It is packaged locally so Harbor can import it through `environment.import_path`.
Remove this file when Harbor exposes a timeout setting, or use stock
`environment.type: e2b` with a plan supporting its 24-hour request.

Read [Harbor concepts](https://docs.harborframework.com/core-concepts) and
[RewardKit judge criteria](https://docs.harborframework.com/core-concepts/rewardkit/judge-criteria)
for the framework features used here.

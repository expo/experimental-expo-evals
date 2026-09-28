# Expo evals

Native [Harbor tasks](https://docs.harborframework.com/core-concepts/tasks/overview)
for Expo SDK 57, paired with and without Expo skills:

- EAS Simulator: create a coffee timer, run it on EAS Simulator, and download a screenshot.
- iOS voice recorder: build a native app with Record and All recordings tabs,
  then run it on a local iOS simulator inside an EAS Workflows macOS machine.

Claude Code runs in the selected sandbox. RewardKit and JEV grade the evidence.

```text
harbor.yaml                       # Task, agent, model, and sandbox selection
harbor-ios.yaml                   # Voice-recorder tasks on EAS staging macOS
pyproject.toml / uv.lock           # Python dependencies and adapter installation
tasks/
  eas-simulator-basic/
    instruction.md                # Prompt
    task.toml                     # Timeouts, credentials, and artifacts
    environment/
      Dockerfile                  # E2B image
      setup.sh                    # Installs EAS CLI
    tests/
      test.sh                     # Calls RewardKit
      rubric.toml                 # JEV criteria
  eas-simulator-basic-expo-skills/ # Same structure, adds skills in setup
  ios-voice-recorder/             # Local iOS app and simulator verification
  ios-voice-recorder-expo-skills/  # Same app task, with Expo skills
environments/
  e2b.py                          # Small E2B timeout override
  eas/                            # EAS Workflows macOS provider
    environment.py                # Harbor lifecycle and SSH transport
    rpc.py                        # Remote command and file operations
    bootstrap.sh                  # Machine initialization
    workflow.yml                  # EAS machine definition
tests/                            # Adapter regression tests
```

Task environment files describe what to install. The root `environments/` package
contains custom sandbox providers that Harbor imports on the host. The EAS
provider shares the task's `setup.sh`; its workflow belongs to the provider.

## Run

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then:

```sh
uv sync --locked
cp .env.example .env
```

Copy `.env` only for a fresh checkout. Fill in Claude Code credentials, E2B,
Expo, and Typesafe keys, then validate or run:

```sh
uv run harbor run --env-file .env -c harbor.yaml --dry-run
uv run harbor run --env-file .env -c harbor.yaml
```

The default runs both EAS Simulator tasks sequentially, one attempt each, with Claude Opus 5.5.
A real run consumes model, E2B, and EAS usage. E2B builds the Dockerfile remotely;
local Docker is unnecessary. Harbor installs the agent.

For Claude subscription usage, run `claude setup-token` and put the resulting
token in `.env`:

```dotenv
CLAUDE_CODE_OAUTH_TOKEN=your-token
CLAUDE_FORCE_OAUTH=1
```

Harbor supports both variables natively. `CLAUDE_FORCE_OAUTH=1` prevents an
existing `ANTHROPIC_API_KEY` from taking precedence and fails if the token is
missing. For API billing, leave the token empty, set the flag to `0`, and provide
`ANTHROPIC_API_KEY`. These settings do not change JEV authentication or billing.
The [OAuth token](https://code.claude.com/docs/en/authentication#generate-a-long-lived-token)
uses your Claude subscription and its usage limits. Some models, including
[Fable](https://code.claude.com/docs/en/model-config#fable-and-usage-credits), can
bill usage credits depending on your plan, including in noninteractive runs.
The hosted dashboard needs the token as a Cloudflare secret; it does not read
this local `.env` file. See its [deployment guide](https://github.com/davidmokos/expo-evals-dashboard/blob/main/DEPLOYMENT.md).

Use native Harbor flags to change the harness, model, or number of attempts:

```sh
uv run harbor run --env-file .env -c harbor.yaml --agent codex --model openai/MODEL_ID
uv run harbor run --env-file .env -c harbor.yaml --n-attempts 3 --n-concurrent 1
```

Replace `MODEL_ID` and set `OPENAI_API_KEY` for Codex. This does not change the judge.

For Codex subscription usage, create a dedicated ChatGPT login:

```sh
mkdir -p -m 700 "$HOME/.codex-evals"
CODEX_HOME="$HOME/.codex-evals" codex -c 'cli_auth_credentials_store="file"' login
```

Sign in with your Pro account, then set `CODEX_AUTH_JSON_PATH` in `.env` to the
absolute path of `~/.codex-evals/auth.json`. Harbor imports this file natively
and prefers it over `OPENAI_API_KEY`. Leave the path empty for API billing.
Keep the file private and outside the repository. Pro usage limits still apply.

Harbor 0.23 discards refreshed sandbox credentials, so this import requires
manual renewal when authentication fails. Run the dedicated login again; do not
share its session with another active Codex process. Hosted runs need the file's
contents installed separately as the dashboard's `CODEX_AUTH_JSON` Worker secret.
See the [dashboard deployment guide](https://github.com/davidmokos/expo-evals-dashboard/blob/main/DEPLOYMENT.md)
and [OpenAI authentication docs](https://learn.chatgpt.com/docs/auth).

## Compare tasks

`harbor.yaml` lists both task directories. Harbor expands its task list, agent
configurations, and attempts into trials. To run only one task, use its native
path override:

```sh
uv run harbor run --env-file .env -c harbor.yaml -p tasks/eas-simulator-basic
uv run harbor run --env-file .env -c harbor.yaml -p tasks/eas-simulator-basic-expo-skills
```

The skills task installs Expo skills from commit
`efa52f0a9d2176db75992736281c77da1b714fa3` into `/skills` during environment setup
and declares `environment.skills_dir = "/skills"` in `task.toml`.
[Harbor registers these task skills](https://docs.harborframework.com/core-concepts/tasks/skills)
with Claude Code or Codex. The baseline has no skills directory.

Keep prompts and rubrics identical for the EAS Simulator comparison. Each task is
self-contained; there is no inheritance or task-generation layer. Skill versions
are pinned in the setup script because build-installed skills do not receive
Harbor's per-skill runtime lock entries.

The dashboard discovers tasks from Git. Select and run each task using the
same commit, harness, model, and attempt count, then compare their results in Runs.
The dashboard uses `metadata.display_name` in `task.toml` for task labels;
folder names remain the task IDs. Harbor accepts this as ordinary metadata.

## Local iOS tasks on EAS Workflows

The voice-recorder tasks require macOS, Xcode, and a local iOS simulator.
`harbor-ios.yaml` selects the EAS provider and staging. The Harbor controller
needs `eas`, `ssh`, and `git` on its PATH:

```sh
npm install --global eas-cli@24.8.0
uv run harbor run --env-file .env -c harbor-ios.yaml --dry-run
uv run harbor run --env-file .env -c harbor-ios.yaml
```

EAS execution is experimental. The staging smoke test reached a running macOS
worker, but SSH hung while opening a session after successful authentication.
Harbor cannot start the agent until that connection works. An end-to-end app
run and the provider's command/file transport are not yet verified.

Set `EXPO_TOKEN_STAGING` and `EAS_PROJECT_ID_STAGING` in `.env` for staging.
Our staging project is `@expo/expo-evals-test`, ID
`33f8931f-1782-485d-b2c3-1b11961d5b0a`.
Production uses `EXPO_TOKEN` and `EAS_PROJECT_ID`; select it with
`--ek stage=production`. Production SSH relay support must be enabled by EAS.
The adapter never substitutes a production token for a missing staging token.
Project selection can also use native `environment.kwargs.project_id`.

Each trial starts an isolated macOS workflow, runs the task's `environment/setup.sh`,
and connects Harbor over EAS SSH. The agent builds the app locally on that machine.
The task setup installs a pinned Maestro CLI and boots an iPhone simulator.
The verifier runs a fixed UI flow and supplies its output, app source, and agent
trace to JEV. Screenshots are saved for human review; the pinned JEV adapter only
accepts text. The flow checks recording controls and persistence, not audible
microphone or speaker quality.

The skills task adds a required `expo-overview` usage criterion and reports
`expo-native-ui` and `expo-ui` usage as zero-weight diagnostic criteria.
Its overall score includes the extra required criterion. Compare the shared
app-quality criteria directly when comparing the two variants.

## Results

Results, traces, verifier output, and artifacts go in Git-ignored `jobs/`.
Running an eval makes no commit. Open Harbor's viewer with `uv run harbor view jobs`.
The hosted runner and viewer live in
[expo-evals-dashboard](https://github.com/davidmokos/expo-evals-dashboard).

Both EAS Simulator tasks use the same six criteria. Four are binary: SDK 57, coffee timer
implementation, EAS Simulator launch, and a downloaded screenshot. Two use JEV's
native rubric scale from 0 to 4: ease of starting the simulator and reliability
of the simulator workflow. Higher is better; the levels describe failed or
unevidenced execution, substantial problems, troubleshooting, minor recovery,
and a smooth run. Normal help lookup and build or session polling do not lower
the score.

RewardKit normalizes each rubric score by dividing it by 4, then averages all
six criteria with equal weight. For example, four binary passes and scale scores
of 3 and 2 produce `(1 + 1 + 1 + 1 + 0.75 + 0.5) / 6 = 0.875`.
The total measures partial credit, not a pass/fail decision. `reward-details.json`
keeps each normalized value and JEV's raw result. Startup ease scores 0 without
evidence that the app launched; reliability scores 0 unless both launch and
screenshot download are evidenced.

The EAS Simulator rubric judges the recorded trace, not independent visual or interactive tests.
`test.sh` uses RewardKit's formatter with a 16,000-token budget to fit JEV's input
limits; grading criteria stay in `rubric.toml`.

## Why the adapter code exists

[Harbor 0.23 hardcodes E2B's lifetime to 24 hours](https://github.com/harbor-framework/harbor/blob/v0.23.0/src/harbor/environments/e2b.py#L221-L236).
It has no constructor option to override it. `environments/e2b.py` changes that
request to one hour for [E2B's base plan](https://docs.e2b.dev/sandbox). This is the
sandbox's maximum lifetime; Harbor still stops it when the trial finishes. Task
timeouts do not change the E2B creation request. Remove this override when Harbor
supports the setting.

Python implements the experimental EAS provider and remote operations; Bash only
prepares the macOS worker. Production and staging use separate credentials and
project IDs. SSH session readiness and command transport still need end-to-end
verification. The default `harbor.yaml` continues to use E2B.

`pyproject.toml` pins Harbor and RewardKit and installs the adapters, as required
by Harbor's [custom sandbox import mechanism](https://docs.harborframework.com/core-concepts/sandboxes/custom-sandboxes).
The RewardKit pin includes JEV support. `uv.lock` keeps dependency versions
consistent locally and in the hosted runner. Neither file defines the task format.

Validate without starting an eval:

```sh
uv run harbor run --env-file .env -c harbor.yaml --dry-run
uv run python -c 'from pathlib import Path; from rewardkit import discover; print({p.parent.name: len(discover(p)) for p in Path("tasks").glob("*/tests")})'
uv run python -m unittest discover -s tests -p 'test_*.py'
```

# Expo evals

One native [Harbor](https://docs.harborframework.com/core-concepts) task: create an
Expo SDK 57 coffee timer, run it on EAS Simulator, and download a screenshot.
Claude Code runs in E2B. RewardKit uses JEV to grade the recorded agent trace.

```text
harbor.yaml                           # Agent, model, environment, and task
tasks/coffee-timer/
  instruction.md                      # Prompt
  task.toml                           # Timeouts, credentials, and artifacts
  environment/Dockerfile              # E2B image
  environment/setup.sh                # Installs EAS CLI
  tests/test.sh                       # Runs RewardKit
  tests/rubric.toml                    # JEV criteria
```

## Run

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then:

```sh
uv sync --locked
cp .env.example .env
```

Copy the environment file only for a fresh checkout. Fill in the Anthropic, E2B,
Expo, and Typesafe keys, then validate or run:

```sh
uv run harbor run --env-file .env -c harbor.yaml --dry-run
uv run harbor run --env-file .env -c harbor.yaml
```

The default is one attempt with Claude Opus 5.5. A real run consumes model, E2B,
and EAS usage. E2B builds the Dockerfile remotely; local Docker is unnecessary.
Harbor installs the agent. The task setup installs EAS CLI, with no Expo skills
installed for the baseline.

Use Harbor's flags to change the agent, model, or number of attempts:

```sh
uv run harbor run --env-file .env -c harbor.yaml --agent codex --model openai/MODEL_ID
uv run harbor run --env-file .env -c harbor.yaml --n-attempts 3 --n-concurrent 1
```

Replace `MODEL_ID` with an available model and set `OPENAI_API_KEY` for Codex.
Changing the agent model does not change the JEV judge.

## Compare skills

Run the same task and model with and without Expo skills using Harbor's native
`--skills` option. Harbor resolves this source and records its Git commit and per-skill
hashes in the job and trial lock files:

```sh
uv run harbor run --env-file .env -c harbor.yaml --job-name coffee-baseline
uv run harbor run --env-file .env -c harbor.yaml --job-name coffee-expo-skills \
  --skills https://github.com/expo/skills/tree/main/plugins/expo/skills
```

Choose new job names when repeating the comparison. Harbor resolves the skills
and installs them for the selected agent. Future jobs may resolve newer skills;
inspect the saved lock files when comparing results. Harbor 0.23 requires a
named ref here; its resolver does not accept a raw commit SHA in the URL.

## Results

Harbor writes results, agent traces, verifier output, and downloaded artifacts
under `jobs/`. This directory is ignored by Git; running an eval makes no commit.
Open Harbor's built-in viewer with:

```sh
uv run harbor view jobs
```

The separate [expo-evals-dashboard](https://github.com/davidmokos/expo-evals-dashboard)
repository contains the hosted viewer and its deployment code. This repository
contains native task definitions and can run directly with Harbor.

The rubric requires evidence of all four outcomes: SDK 57, a working coffee timer
implementation, a successful EAS Simulator launch, and a downloaded screenshot.
Its `all-pass` aggregation awards 1 only when every criterion passes. The judge
reads the agent trace; its score is not an independent visual or interactive test.

`tests/test.sh` calls RewardKit's `format_trajectory(max_tokens=16000)` to reduce
trace size before grading. A saved run's raw trace was roughly 280,000 tokens;
the formatted trace was roughly 7,400, using a standard tokenizer estimate.
JEV allows 32,000 tokens for the trace plus the longest criterion and 64,000
for the trace plus all criteria. Its exact token count can differ. Formatting
addresses size, not JSON compatibility; the rubric contains the criteria.

## Compatibility code

`e2b_environment.py` changes Harbor 0.23's default sandbox lifetime from 24 hours
to one hour for the current E2B plan. Harbor still owns the environment lifecycle.
The RewardKit dependency pins the upstream commit that adds JEV support.

`eas_environment.py` and `eas_provider/` retain the deferred EAS Workflows
prototype. It is not selected by `harbor.yaml`. Its macOS bootstrap was tested,
but remote execution still needs EAS SSH relay availability and PTY compatibility
work before an end-to-end eval can pass. It is not a working alternative yet.

Validate changes without starting a paid eval:

```sh
uv run harbor run --env-file .env -c harbor.yaml --dry-run
uv run python -c 'from rewardkit import discover; print(discover("tasks/coffee-timer/tests"))'
uv run python -m unittest discover -s tests -p 'test_*.py'
```

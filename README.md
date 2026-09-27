# Expo evals

One native [Harbor task](https://docs.harborframework.com/core-concepts/tasks/overview):
create an Expo SDK 57 coffee timer, run it on EAS Simulator, and download a screenshot.
Claude Code runs in E2B. RewardKit uses JEV to grade the agent trace.

```text
harbor.yaml                       # Task, agent, model, and sandbox selection
pyproject.toml / uv.lock           # Python dependencies and adapter installation
tasks/coffee-timer/
  instruction.md                  # Prompt
  task.toml                       # Timeouts, credentials, and artifacts
  environment/
    Dockerfile                    # E2B image
    setup.sh                      # Installs EAS CLI
  tests/
    test.sh                       # Calls RewardKit
    rubric.toml                   # JEV criteria
environments/
  e2b.py                          # Small E2B timeout override
  eas/                            # Deferred macOS provider
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

Copy `.env` only for a fresh checkout. Fill in the Anthropic, E2B, Expo, and
Typesafe keys, then validate or run:

```sh
uv run harbor run --env-file .env -c harbor.yaml --dry-run
uv run harbor run --env-file .env -c harbor.yaml
```

The default is one attempt with Claude Opus 5.5. A real run consumes model, E2B,
and EAS usage. E2B builds the Dockerfile remotely; local Docker is unnecessary.
Harbor installs the agent. The task installs EAS CLI without Expo skills.

Use native Harbor flags to change the harness, model, or number of attempts:

```sh
uv run harbor run --env-file .env -c harbor.yaml --agent codex --model openai/MODEL_ID
uv run harbor run --env-file .env -c harbor.yaml --n-attempts 3 --n-concurrent 1
```

Replace `MODEL_ID` and set `OPENAI_API_KEY` for Codex. This does not change the judge.
To compare Expo skills, repeat the run with:

```sh
uv run harbor run --env-file .env -c harbor.yaml \
  --skills https://github.com/expo/skills/tree/main/plugins/expo/skills
```

Harbor installs the skills and records their resolved Git commit and hashes in
its lock files. Future runs can resolve newer skills. Harbor 0.23 requires a
named ref for this URL; its resolver does not accept a raw commit SHA.

## Results

Results, traces, verifier output, and artifacts go in Git-ignored `jobs/`.
Running an eval makes no commit. Open Harbor's viewer with `uv run harbor view jobs`.
The hosted runner and viewer live in
[expo-evals-dashboard](https://github.com/davidmokos/expo-evals-dashboard).

The rubric awards 1 only when all four criteria pass: SDK 57, coffee timer
implementation, EAS Simulator launch, and a downloaded screenshot. It judges
the recorded trace, not independent visual or interactive tests.
`test.sh` uses RewardKit's formatter with a 16,000-token budget to fit JEV's input
limits; grading criteria stay in `rubric.toml`.

## Why the adapter code exists

[Harbor 0.23 hardcodes E2B's lifetime to 24 hours](https://github.com/harbor-framework/harbor/blob/v0.23.0/src/harbor/environments/e2b.py#L221-L236).
It has no constructor option to override it. `environments/e2b.py` changes that
request to one hour for [E2B's base plan](https://docs.e2b.dev/sandbox). This is the
sandbox's maximum lifetime; Harbor still stops it when the trial finishes. Task
timeouts do not change the E2B creation request. Remove this override when Harbor
supports the setting.

The EAS provider is a deferred prototype. Python implements the provider and
remote operations; Bash only prepares the macOS worker. EAS SSH relay availability
and PTY compatibility still block an end-to-end run. It is not selected by default.

`pyproject.toml` pins Harbor and RewardKit and installs the adapters, as required
by Harbor's [custom sandbox import mechanism](https://docs.harborframework.com/core-concepts/sandboxes/custom-sandboxes).
The RewardKit pin includes JEV support. `uv.lock` keeps dependency versions
consistent locally and in the hosted runner. Neither file defines the task format.

Validate without starting an eval:

```sh
uv run harbor run --env-file .env -c harbor.yaml --dry-run
uv run python -c 'from rewardkit import discover; print(discover("tasks/coffee-timer/tests"))'
uv run python -m unittest discover -s tests -p 'test_*.py'
```

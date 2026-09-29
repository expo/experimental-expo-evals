# Repository conventions

- Use native Harbor task files, configuration, CLI, and RewardKit. Keep web and hosted runner code in `expo-evals-dashboard`; do not add another task format or grading framework.
- Keep each comparison pair's prompts and shared JEV criteria identical. Skills-specific criteria may be added when requested. Put task-specific setup in each task; skills tasks use Harbor's native `environment.skills_dir`. Pin downloaded skills in setup scripts. Do not add custom scenarios or task inheritance.
- Keep provider code under `environments/` and task setup under `tasks/<task>/environment/`. E2B's one-hour override is needed while Harbor 0.23 hardcodes 24 hours. EAS staging is experimental until a full eval passes; its public SSH relay can fail while jobs are running.
- Validate with Harbor's `--dry-run`, RewardKit's `discover()`, and adapter tests. Real runs consume model and EAS usage; default to one attempt and one concurrent trial.
- Keep credentials in ignored `.env` files and results in ignored `jobs/`. State what the verifier actually checks: EAS Simulator tasks grade the trace; iOS tasks also collect source and run a fixed Maestro flow. Neither proves audible recording quality.
- Commit messages: one lowercase-starting line, no conventional prefixes, body, or co-author lines. PR titles use a bracketed scope such as `[evals]`; do not credit AI in the body.

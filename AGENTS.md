# Repository conventions

- Use native Harbor task files, configuration, CLI, and RewardKit. Keep web and hosted runner code in `expo-evals-dashboard`; do not add another task format or grading framework.
- Keep both task prompts and JEV rubrics identical unless requested. Put task-specific setup in each task; the skills task uses Harbor's native `environment.skills_dir`. Pin downloaded skills in its setup script. Do not add custom scenarios or task inheritance.
- Keep provider code under `environments/` and task setup under `tasks/<task>/environment/`. E2B's one-hour override is needed while Harbor 0.23 hardcodes 24 hours. EAS remains a deferred prototype until its SSH transport works end to end.
- Validate with Harbor's `--dry-run`, RewardKit's `discover()`, and adapter tests. Real runs consume model and EAS usage; default to one attempt and one concurrent trial.
- Keep credentials in ignored `.env` files and results in ignored `jobs/`. The judge grades the trace; its score does not independently prove visual or interactive correctness.
- Commit messages: one lowercase-starting line, no conventional prefixes, body, or co-author lines. PR titles use a bracketed scope such as `[evals]`; do not credit AI in the body.

# Repository conventions

- Keep `tasks/coffee-timer/instruction.md` and the JEV rubric unchanged unless requested.
- Use native Harbor tasks, configuration, CLI commands, and RewardKit. Keep web and hosted runner code in the separate dashboard repository. Do not add another task format or grading framework.
- E2B is the default. The one-hour adapter is required with Harbor 0.23 on the current E2B plan. Keep the EAS Workflows adapter as a deferred prototype until its transport works end to end.
- Keep the baseline free of Expo skills. Compare skills with Harbor's native `--skills` option; its lock files record the resolved source commit and skill hashes.
- Use RewardKit's existing trajectory formatter to keep judge inputs within its token budget. Put grading criteria in the rubric.
- Validate changes with Harbor's `--dry-run`, RewardKit's `discover()`, and the adapter tests. Real evals consume model and EAS usage; default to one attempt and one concurrent trial.
- Keep credentials in ignored `.env` files and results in ignored `jobs/`. Never commit secrets or generated run artifacts.
- The judge grades the recorded trace. Do not describe its score as proof of visual or interactive correctness.
- Commit messages must be one lowercase-starting line without conventional prefixes or co-author lines.
- PR titles use a bracketed scope such as `[evals]`; do not credit AI in the body.

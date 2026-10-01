# Repository conventions

- Use native Harbor task files, configuration, CLI, and RewardKit. Keep web and hosted runner code in `expo-evals-dashboard`; do not add another task format or grading framework.
- Keep each comparison pair's prompts and shared JEV criteria identical. Skills-specific criteria may be added when requested. Put task-specific setup in each task; skills tasks use Harbor's native `environment.skills_dir`. Use `npx skills` for pinned Expo skills; let `argent init` install its bundled skills. Do not add custom scenarios or task inheritance.
- Keep provider code under `environments/` and task setup under `tasks/<task>/environment/`. E2B's one-hour override is needed while Harbor 0.23 hardcodes 24 hours. EAS uses an outbound HTTPS worker through the dashboard runner; keep production and staging credentials separate and never replay uncertain commands.
- Validate with Harbor's `--dry-run`, RewardKit's `discover()`, and adapter tests. Real runs consume model and EAS usage; default to one attempt and one concurrent trial.
- Keep credentials in ignored `.env` files and results in ignored `jobs/`. State what the verifier actually checks: EAS Simulator tasks grade the trace; the basic local iOS task adds a screenshot file check; voice-recorder tasks collect source and run a fixed Maestro flow. Text-only JEV cannot inspect screenshots or prove audible recording quality.
- Commit messages: one lowercase-starting line, no conventional prefixes, body, or co-author lines. PR titles use a bracketed scope such as `[evals]`; do not credit AI in the body.

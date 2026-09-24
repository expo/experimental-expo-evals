# Repository conventions

- Keep the eval prompt in `tasks/coffee-timer/instruction.md` unchanged unless requested.
- Keep this a minimal Harbor example. Use its CLI and RewardKit; do not add a custom runner or grading framework. The E2B timeout adapter is a temporary compatibility exception.
- The TanStack Start frontend runs on Cloudflare Workers. D1 stores drafts, revisions, and run records; explicit commits publish native task files to GitHub. A Cloudflare Container invokes the Harbor CLI with E2B. Keep Harbor's native file format and grading engine; do not introduce another eval format.
- Require company authentication in production. Never silently overwrite concurrent edits or force-push task commits. Run immutable committed snapshots and keep credentials server-side.
- Validate with Harbor's `--dry-run` and RewardKit's `discover()` after changes.
- Keep credentials in the ignored `.env`. Never put them in images, configs, or fixtures.
- Eval runs consume model and EAS usage. Default to one attempt and one concurrent trial.
- The judge grades the recorded trace. Do not describe its score as proof of visual or interactive correctness.
- Commit messages must be one lowercase-starting line without conventional prefixes or co-author lines.
- PR titles use a bracketed scope such as `[evals]`; do not credit AI in the body.

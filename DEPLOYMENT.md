# Cloudflare deployment

The web app runs on Workers. D1 stores shared drafts, revisions, and run records.
GitHub stores committed Harbor task files. R2 holds results and artifacts. Each
run starts a Cloudflare Container with Python and the pinned Harbor dependencies;
Harbor creates the agent/verifier sandbox in E2B. Cloudflare Containers require
an account/plan with Containers enabled. Ordinary Workers cannot spawn Harbor.

This deployment uses a Container for the Harbor coordinator only. The task's
`environment/Dockerfile` still describes the E2B environment. Neither runs a
local Expo simulator: the evaluated agent uses EAS Simulator.

## Local setup

From `frontend/`, run `npm ci`, `npm run dev:secrets`, `npm run db:migrate`, then
`npm run dev`. The ignored `.dev.vars` enables authentication bypass only for
loopback URLs. D1/R2 data persists under `.wrangler/state/` between restarts.
Docker is not needed for the editor. Local Containers are disabled in Wrangler's
`dev.enable_containers` setting. Enabling them also requires a callback origin
reachable from Docker, such as an authenticated development tunnel or
`http://host.docker.internal:3000` with suitable host binding.

The app seeds the coffee-timer task on first use. Shared drafts are stored as
the exact six native file contents; the UI doesn't translate them to another
eval format. Draft saves do not alter local repository files. The CLI continues
to read local files, so pull Git changes before running a shared task locally.

## Provision once

Run these from `frontend/` in the intended account:

```sh
npx wrangler whoami
npx wrangler d1 create eas-simulator-evals
npx wrangler r2 bucket create eas-simulator-eval-artifacts
```

This checkout already points to the Expo account and its `eas-simulator-evals`
D1 database and `eas-simulator-eval-artifacts` R2 bucket. Run the provisioning
commands only when deploying to a new account, then update the database ID and
`account_id` in `wrangler.jsonc`. Keep the R2 bucket private.

Create or select a private GitHub repository with an existing `main` branch.
Commit the project there through your normal workflow first. Configure
`GITHUB_REPOSITORY=owner/repository` and `GITHUB_BRANCH` in Wrangler vars. The
Commit button updates task files only, using GitHub's atomic
`createCommitOnBranch` operation. It cannot create an empty Git repository.
Configure GitHub App authentication below, or use a fine-grained token restricted
to that repository with Contents read/write permission as `GITHUB_TOKEN`.
Branch protection still applies.
The private repository for this checkout is
[`davidmokos/eas-simulator-evals`](https://github.com/davidmokos/eas-simulator-evals).

## GitHub bot

This deployment uses the private
[`davidmokos-eas-evals`](https://github.com/settings/apps/davidmokos-eas-evals)
app, installed only on `davidmokos/eas-simulator-evals`.

Register a private GitHub App under the account that owns the repository. Give
it repository **Contents: read and write**; Metadata read is included by GitHub.
Disable webhooks and leave user authorization callbacks empty. Install it on
only the eval repository. GitHub provides the app's bot identity automatically.

Set `GITHUB_APP_ID` to its App ID or client ID and
`GITHUB_APP_INSTALLATION_ID` to the numeric ID in the installation settings URL.
Generate a private key in the app settings and put its complete PEM value in
`GITHUB_APP_PRIVATE_KEY`. Both downloaded PKCS#1 keys and PKCS#8 keys work.
For local development, keep these in the ignored root `.env` and run
`npm run dev:secrets` from `frontend/`. A PEM can be a double-quoted multiline
dotenv value. Never commit the downloaded key.

Set the same three values as Worker secrets. For the private key, piping the
downloaded file avoids putting it into shell history:

```sh
npx wrangler secret put GITHUB_APP_ID
npx wrangler secret put GITHUB_APP_INSTALLATION_ID
npx wrangler secret put GITHUB_APP_PRIVATE_KEY < /path/to/downloaded.private-key.pem
```

The Worker exchanges an app JWT for a short-lived installation token restricted
to the configured repository and Contents write. It refreshes the token before
expiry. Complete app credentials take precedence over the optional `GITHUB_TOKEN`
fallback. Commits are authored as the app bot; the editor records the employee
who saved each revision in D1. No GitHub credential is sent to the browser or
Harbor runner.

See [GitHub App registration](https://docs.github.com/en/apps/creating-github-apps/registering-a-github-app/registering-a-github-app)
and [installation authentication](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/authenticating-as-a-github-app-installation).

## Dashboard login

The current deployment uses a shared username/password login. Set
`DASHBOARD_USERNAME`, `DASHBOARD_PASSWORD`, and a random `SESSION_SECRET` as
Worker secrets. Use a strong password and at least 32 random bytes for the
session secret. The login creates a signed, HttpOnly cookie that expires after
12 hours. Sign out clears that cookie. Every signed-in user has the same editor
and runner permissions, and revisions use the shared username as their author.

The initial admin credentials are in the ignored local
`work/dashboard-login.txt` and root `.env`. They are never committed. Rotate the
password by updating its Worker secret; rotate `SESSION_SECRET` to invalidate
existing sessions. The app denies access if no authentication method is
configured.

## Optional company authentication

Create a Cloudflare Access self-hosted application for the app hostname with
your company's identity provider and employee policy. Set these Wrangler vars:

```text
APP_ORIGIN=https://<your-app-hostname>
ACCESS_TEAM_DOMAIN=https://<your-team>.cloudflareaccess.com
ACCESS_AUD=<the Access application's audience tag>
LOCAL_DEV=false
```

The Worker verifies the Access JWT signature, issuer, audience, and email. It
does not trust an email header alone. Access protects the app's pages, server
functions, and result downloads. Configured Access takes precedence over the
shared login. All authenticated employees can edit and run tasks
in this first version; there are no per-task roles.

Give `/api/runner-callback/*` its own higher-specificity Access application with
a Bypass policy so the Python runner can reach it. That endpoint independently
requires an unguessable per-run Bearer token, stored hashed in D1, and accepts
only that run's uploads/status. Do not bypass Access for the app or other API
routes. If using workers.dev as the callback origin, ensure its Access setup
also permits this callback path; the Worker still authenticates all user routes.

Wrangler's standard OAuth login may not allow Access administration. For API
setup, use an account-scoped `CF_ACCESS_API_TOKEN` with Access Apps and Policies
Write and Access Organizations Read. Keep it in the ignored root `.env` during
setup only; it is not a Worker secret. Access can also be configured in the
Cloudflare dashboard.

## Secrets and deployment

Set secrets interactively without putting their values in source files:

```sh
# Only when using a personal token instead of the GitHub App above:
# npx wrangler secret put GITHUB_TOKEN
npx wrangler secret put DASHBOARD_USERNAME
npx wrangler secret put DASHBOARD_PASSWORD
npx wrangler secret put SESSION_SECRET
npx wrangler secret put ANTHROPIC_API_KEY
npx wrangler secret put E2B_API_KEY
npx wrangler secret put EXPO_TOKEN
npx wrangler secret put TYPESAFE_API_KEY
# Optional, for Codex evals:
npx wrangler secret put OPENAI_API_KEY
```

`ASSIST_MODEL` defaults to `claude-opus-5-5`. The assistant uses Anthropic's API
and returns proposed file changes only. The eval agent/model remains selectable
per run. AI calls and evals consume provider usage.

Start Docker, then deploy:

```sh
npm run check
npm test
npm run db:migrate:remote
npm run deploy
```

`npm run deploy` builds the TanStack app, then runs `wrangler deploy`. Wrangler
builds/uploads the Container image using the repository root as its context.
The root `.dockerignore` permits only pinned dependencies, the E2B adapter, and
runner source; it excludes `.env`, `.dev.vars`, local jobs, and Git metadata.
No credentials are baked into the image.

After deployment, verify sign-in, save/reload a draft, commit to the
chosen branch, and launch one eval. Its result should remain visible after the
runner stops. Browser/Node tests and a local image build do not prove a live
Cloudflare/E2B run; test that separately after the deployment is configured.

## Saving and conflicts

Each save sends the D1 revision the editor loaded. A database compare-and-swap
rejects stale updates and saves all file contents plus revision history in one
transaction. It never silently overwrites another person's saved draft.

Commit reserves that draft temporarily, reads the current Git branch, and
compares base, draft, and remote file contents. Changes to different files merge
automatically. Edits to the same file require the user to choose Draft, Git, or
edited content. Resolutions are tied to the displayed Git head. If the branch
changes again, the app checks again. The final GitHub write includes the expected
head SHA, so it cannot force past a concurrent commit. If the files already
match Git, the app records the existing commit without adding an empty commit.

D1 and GitHub cannot share a transaction. If Git accepts a commit and the D1
update fails, the UI reports the commit SHA. Retrying reconciles the remote files;
the task is not considered runnable until D1 records its committed revision.

## Run lifecycle

The app stores a run's commit SHA and file snapshot before starting its Container.
Only one run can be queued/running at a time, enforced by a D1 unique index.
Container storage also records the accepted run ID to avoid launching it twice.
If startup acknowledgement is lost, the app keeps the slot reserved while it
waits for a callback; it does not automatically retry a potentially billable run.

The runner launches native `harbor run` with one attempt, one concurrent trial,
and E2B. It uploads results to R2 and updates D1 before completion. Container
disk is disposable, so completed results never rely on it. Files have per-file
and total upload limits; credentials and hidden files are excluded. The runner
has a one-hour timeout. A five-minute scheduled check marks runs without a final
callback as interrupted after 80 minutes. It does not rerun them.

Harbor owns execution and grading. This app adds editing, persistence,
authentication, and process supervision. JEV grades the recorded trace; its
score is not independent proof of visual or interactive correctness.

References: [TanStack on Workers](https://developers.cloudflare.com/workers/framework-guides/web-apps/tanstack-start/),
[Cloudflare Containers](https://developers.cloudflare.com/containers/get-started/),
[Access JWT validation](https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/authorization-cookie/validating-json/),
[Harbor task format](https://docs.harborframework.com/core-concepts/tasks/overview).

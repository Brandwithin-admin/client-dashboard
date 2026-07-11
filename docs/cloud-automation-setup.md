# Cloud Automation Setup

The hourly dashboard sync runs from GitHub Actions in `.github/workflows/dashboard.yml`.
It does not depend on a laptop being awake once the required secrets are added.

## Required GitHub repository secrets

Add these in the repository:

`Settings` -> `Secrets and variables` -> `Actions` -> `New repository secret`

- `SLACK_BOT_TOKEN`
- `CLICKUP_API_TOKEN`
- `OPENAI_API_KEY`

Optional repository variables:

- `OPENAI_MODEL` defaults to `gpt-4.1-mini`
- `BRANDWITHIN_COMMIT_AUTHOR_NAME` defaults to `Brandwithin Dashboard Bot`
- `BRANDWITHIN_COMMIT_AUTHOR_EMAIL` defaults to `brandwithin-dashboard-bot@users.noreply.github.com`

## Slack token

Use a Slack bot token that can read `#client-mariangela-parodi`.

Minimum bot token scopes:

- `channels:history`
- `groups:history`
- `users:read`

If the channel is private, invite the Slack app/bot to the channel. The token usually starts with `xoxb-`.

## ClickUp token

Use a ClickUp personal API token from a user who can read and update list `901615501737`.
The token is sent as the ClickUp `Authorization` header and usually starts with `pk_`.

## OpenAI token

Use an OpenAI project API key that can call the model in `OPENAI_MODEL`.

## Manual backfill

In GitHub, open `Actions` -> `Brandwithin client dashboard sync` -> `Run workflow`.
Set `backfill_hours` to `12`, `24`, or `72` when you want the first cloud run to catch missed Slack messages.

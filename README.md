# Morning Market Briefing

An automated pre-market newsletter. Every weekday morning a GitHub Action uses the Anthropic API (Claude with web search) to gather overnight market news, index and yield moves, and notable price movers, then emails a formatted briefing.

Each briefing is also saved as Markdown in [`DailyBriefings/`](DailyBriefings/), so the folder doubles as a running archive.

## How it works

1. A scheduled GitHub Actions workflow runs Mon–Fri at 10:09 AM ET (14:09 UTC).
2. Claude searches the web and writes the briefing.
3. The briefing is emailed through Gmail and committed to `DailyBriefings/`.

## Setup

1. Fork or clone this repo.
2. In **Settings → Secrets and variables → Actions**, add:

| Secret | Value |
|---|---|
| `ANTHROPIC_API_KEY` | Your Anthropic API key |
| `GMAIL_SENDER` | The Gmail address that sends the briefing |
| `GMAIL_APP_PASSWORD` | A Gmail app password (Google Account → Security → 2-Step Verification → App passwords) |

3. Open the **Actions** tab, choose **Morning Market Briefing**, and click **Run workflow** to test.

To change the time, edit the cron line in `.github/workflows/morning-briefing.yml`.

# Morning Market Briefing

Runs every weekday at 10:09 AM ET via GitHub Actions. Fetches market news and price movers using the Anthropic API (web search), then emails a formatted briefing to Sanjayja@gmail.com.

## Setup

### 1. Create a GitHub repository

Go to https://github.com/new, create a **private** repository named `morning-market-briefing`.

### 2. Add GitHub Secrets

In your new repo, go to **Settings → Secrets and variables → Actions → New repository secret** and add these three:

| Secret name | Value |
|---|---|
| `ANTHROPIC_API_KEY` | Your Anthropic API key |
| `GMAIL_SENDER` | Your Gmail address (e.g. you@gmail.com) |
| `GMAIL_APP_PASSWORD` | Your Gmail App Password (not your regular password) |

> To create a Gmail App Password: Google Account → Security → 2-Step Verification → App passwords

### 3. Push this repo

```bash
cd C:\Users\Sjain\Documents\morning-market-briefing
git init
git add .
git commit -m "Initial commit"
git remote add origin https://github.com/YOUR_USERNAME/morning-market-briefing.git
git push -u origin main
```

### 4. Verify

- Go to your repo → **Actions** tab
- Click **Morning Market Briefing** → **Run workflow** to test immediately
- Check your inbox within ~5 minutes

## Schedule

Runs automatically Mon–Fri at 10:09 AM ET (14:09 UTC). Adjust the cron in `.github/workflows/morning-briefing.yml` if needed.

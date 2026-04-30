# Mujeres de Acción — Weekly Grant Research Workflow

Every Monday at 11 AM PST this workflow searches for currently open grant opportunities
relevant to Mujeres de Acción's mission and emails a formatted summary. Only **new**
grants (not previously reported) are included. If nothing new is found, the email says so.

---

## How It Works

1. **GitHub Actions** triggers the workflow on schedule (`cron: '0 19 * * 1'`).
2. `scripts/grant_research.py` calls the **Anthropic API** with web search enabled,
   asking it to find open grants matching MDA's profile.
3. Results are compared against `data/seen_grants.json` — a persistent list of every
   grant ever reported. New-only grants are emailed.
4. `seen_grants.json` is committed back to the repo so state survives across runs.

---

## Repository Structure

```
.
├── .github/
│   └── workflows/
│       └── grant_research.yml   # Scheduled workflow definition
├── data/
│   └── seen_grants.json         # Auto-updated; tracks reported grants
├── scripts/
│   └── grant_research.py        # Core script
└── README.md
```

---

## One-Time Setup

### 1. Required GitHub Secrets

Go to your repo → **Settings → Secrets and variables → Actions → New repository secret**
and add each of the following:

| Secret | Description |
|--------|-------------|
| `ANTHROPIC_API_KEY` | Your Anthropic API key (get it at console.anthropic.com) |
| `EMAIL_FROM` | The Gmail (or SMTP) address that sends the report |
| `EMAIL_TO` | The recipient address (can be a distribution list) |
| `EMAIL_PASSWORD` | App password for the sending account (see Gmail note below) |
| `SMTP_HOST` | SMTP server hostname — defaults to `smtp.gmail.com` if omitted |
| `SMTP_PORT` | SMTP port — defaults to `587` if omitted |

### 2. Gmail App Password (if using Gmail)

Gmail requires an **App Password** rather than your account password:
1. Enable 2-Step Verification on the sending Google account.
2. Go to [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords).
3. Create an app password for "Mail" and paste it as `EMAIL_PASSWORD`.

### 3. Workflow Permissions

The workflow commits `seen_grants.json` back to the repo. Ensure **Actions** have
write permissions:
- Repo → **Settings → Actions → General → Workflow permissions** → select
  **Read and write permissions**.

---

## Daylight Saving Time Note

GitHub cron runs on UTC and does not auto-adjust for DST.

| Period | Cron setting | Local time |
|--------|-------------|------------|
| Standard time (Nov–Mar) | `0 19 * * 1` | 11:00 AM PST |
| Daylight time (Mar–Nov) | `0 18 * * 1` | 11:00 AM PDT |

The workflow is currently set to `19:00 UTC`. During daylight saving it will fire at
12:00 PM PDT instead of 11:00 AM. Update the cron to `0 18 * * 1` in summer if
strict timing matters.

---

## Manual Trigger

You can run the workflow at any time without waiting for Monday:
- Go to **Actions → Weekly Grant Research → Run workflow**.

---

## Resetting the Seen Grants List

To force all grants to be treated as new (e.g. after a long gap or a major org pivot):
```bash
echo "{}" > data/seen_grants.json
git add data/seen_grants.json
git commit -m "chore: reset seen grants"
git push
```

# Atlas — Private Analytics Job Agent

A runnable **private-first prototype** for Manager/Senior Manager analytics job discovery, hiring-contact research, job approval, Gmail draft scheduling, reply monitoring and outreach tracking.

**Status:** Local demo is runnable without API keys. Live integrations are implemented as adapters but **not connected or end-to-end verified with real credentials**. Scheduled sending and automated follow-ups are **off** by default; no emails are sent in demo mode. Five fictional sample cards are labelled DEMO, not presented as real job postings.

## Quick start — local demo (no external credentials)

Requires Python 3.11+.

```bash
python -m venv .venv
# Windows PowerShell: .venv\\Scripts\\Activate.ps1
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # Windows: copy .env.example .env
python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

Open **http://127.0.0.1:8000**. In demo mode you can explore the dashboard, approve fictional job cards, see proposed email copy and generated PDF resumes, and use the activity tracker. No actual job discovery, Gmail messages, or RocketReach requests take place in demo mode. Demo mode has no authentication: **do not expose the demo server to the public Internet**.

Run tests:

```bash
python -m pytest -q
```

## Architecture

| Component | Implementation | Notes |
|---|---|---|
| Dashboard | Responsive HTML/CSS/vanilla JS | Job cards, recruiter profiles, outreach queue, analytics, events, settings |
| API | Python FastAPI | Session cookie restricted to configured Google login in live mode |
| Database | SQLAlchemy / SQLite locally; PostgreSQL in production | Jobs, contacts, scheduled outreach, encrypted OAuth tokens, events |
| Search | Two independent engines: Serper public index + OpenAI Responses API web_search | Source-backed URLs required; no logged-in LinkedIn scraping |
| Contact research | Public LinkedIn-profile index + RocketReach API | Profiles labelled potential unless verified; contacts/emails may be unavailable |
| Resume tailoring | Fact-locked ReportLab PDF | Reorders actual master-resume achievements to match JD; no invented facts |
| Gmail | Google OAuth and Gmail REST API | Drafts created on approval, live edits preserved, scheduled send |
| Telegram | Telegram Bot API | Alert cards with Approve/Reject; webhook checks Telegram secret and chat ID |
| Scheduling | Protected `/api/cron/tick` | Trigger periodically with cloud scheduler; see deployment section |
| Reply tracking | Gmail thread polling before sends | Stops follow-ups on inbound replies; holds follow-up if Gmail check fails |

**Sources and evidence:** Hiring post link, posting age (when known), original poster, recruiter/hiring contact, professional email confidence, and matching rationale are stored separately. Search snippets cannot reliably provide all these fields. Do not interpret an indexed result as proof that a position remains open.

## Configuration

- `config/settings.yaml`: schedule, location/title scope, email window, holidays, send cap, follow-up delay and number of attempts.
- `config/job_scope.md`: the job scope and search constraints.
- `config/job_search_prompt.md`: your full original job-hunting research prompt, used by the independent ChatGPT web-search engine. You can edit and save a private override from the dashboard.
- `config/resume_facts.yaml`: approved factual resume content, extracted from `data/master_resume.pdf`.
- `.env`: credentials, database connection and app secrets (never commit it).

**Default:** search weekdays 09:00 IST; 45-day lookback, prefer past 48 hours; max 5 shortlist; weekdays Mon-Fri; send window 09:00–15:00 IST; initial send at next eligible 10:30 IST; max 8 messages/day; automatic sending **disabled**; follow-ups 48 elapsed hours, max 2, **disabled** until tested. Calendar dates use ISO `YYYY-MM-DD`. Config is loaded at process start; restart after changes.

## Connect services (private live mode)

1. Create a Google Cloud OAuth 2.0 **Web application** client. Enable Gmail API. Add redirect URI exactly `http://localhost:8000/api/auth/google/callback` (for local testing), and later your private HTTPS production URL `/api/auth/google/callback`. Google sign-in requests only basic identity scopes. Gmail read + compose permissions are requested later through a separate Connect Gmail step. Google may require test users, app verification or other consent configuration before real use.
2. Add `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `APP_BASE_URL`, and `ALLOWED_GOOGLE_EMAIL` in `.env`. Generate `SESSION_SECRET` (`python -c "import secrets; print(secrets.token_hex(48))"`) and `TOKEN_ENCRYPTION_KEY` (`python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`). Do **not** paste secrets into a chat, web frontend or Git repo.
3. Create your Telegram bot with BotFather. Start a chat with it. Find your private chat ID. Add `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` and a random `TELEGRAM_WEBHOOK_SECRET`. For live HTTPS use Telegram `setWebhook` for `https://YOUR-DOMAIN/api/telegram/webhook`, passing the secret token. For local development an HTTPS tunnel may be used *only after securing the endpoint*.
4. Set `SERPER_API_KEY` for indexed job search and/or `OPENAI_API_KEY` for an independent OpenAI Responses API model with the hosted `web_search` tool. Set `OPENAI_SEARCH_MODEL` (default `gpt-5.6`). The **Find jobs** button runs both configured engines separately, deduplicates stored leads, and never fabricates jobs or contact links. ChatGPT Plus does not include API credits; web-search and model calls have separate usage charges. No logged-in LinkedIn scraper is included.
5. Add a real `ROCKETREACH_API_KEY` with API entitlement, and verify the provider's current lookup endpoint/response shape against your plan and official API documentation. This integration is scaffolded but not tested with a live account; it may require adjustments.
6. Set `DATABASE_URL` to a **persistent PostgreSQL** instance for cloud deployment, use `DEMO_MODE=false` and connect with the configured Google account. Gmail OAuth refresh tokens are encrypted before database storage; **keep `TOKEN_ENCRYPTION_KEY` stable** and protect backups.
7. Test a **single approved job and draft using your own email as the recipient**. Confirm Telegram, edited draft subject/body/attachment, sent message ID and reply stop behavior. Only **after** you explicitly authorize live dispatch set `email.scheduled_sending_enabled: true` in `config/settings.yaml`. Separately opt into `email.followup.enabled: true` after a verified dry run.

## Approval, drafting and scheduling

**Your exact requested approval rule:** one job approval authorizes creating the tailored resume and a Gmail draft, and queues the email for the configured send time. No second approval is required. You can edit the Gmail draft; at send time the backend fetches and sends **that saved draft**. Deleted/missing draft or empty subject/body stops sending and requires review. You may cancel scheduled unsent mail in the dashboard. Sending never happens immediately on approval.

If there's no professional email or Gmail has not been connected, the job is approved but placed in **pending contact** or **awaiting Gmail** instead of pretending it is scheduled.

Follow-ups are *separate Gmail drafts*. The configured 48-hour timer is based on elapsed hours (or optional business hours), adjusted to the next permitted 10:30 working-day slot. A reply in the Gmail thread stops follow-ups. If the thread cannot be checked, sending the follow-up is blocked for manual review. The event log keeps message IDs and dates; no reply content is exported to Telegram.

**Provider limitations:** Gmail UI supports editing drafts, but not a built-in cross-service guarantee that an edited draft remains schedulable: Atlas retains the queue and sends the Gmail draft via API when due. Do **not** use Gmail's own “Schedule send” on the same draft, or you can create a separate schedule outside Atlas's control.

## Scheduled execution and deployment

For local testing, call the endpoint securely to simulate a scheduler:

```bash
curl -H "Authorization: Bearer YOUR_CRON_SECRET" http://127.0.0.1:8000/api/cron/tick
```

For cloud, host the FastAPI API on **Vercel** with environment variables and PostgreSQL, but use a suitable **reliable external scheduler calling `/api/cron/tick` at least every 5 minutes with `Authorization: Bearer <CRON_SECRET>`**. Alternatively a Vercel Pro plan can use an appropriately frequent native cron. Vercel Hobby native cron is generally limited to daily execution; it cannot reliably honor a 10:30 fixed send time with 5-minute queue polling. The provided `vercel.json` intentionally does **not** configure a sub-daily native cron so it won't create a Hobby-plan deployment failure.

Cloud functions have request-time limits. For production loads, split discovery, email polling and dispatch into separate durable queue/worker tasks. This prototype is sized for a single private user and small volumes and needs provider end-to-end testing and operational monitoring before it becomes an unattended production service.

**Important:** Upload to a **private GitHub repository**. Do not deploy publicly or enable real sending before an explicit go-ahead. Prefer secret management in the host deployment. For Google OAuth, the production app must be HTTPS.

## Known limitations

- Live-job discovery requires Serper and/or an OpenAI API key with web-search access; **no real postings are included in the demo**. The AI search engine accepts only job URLs surfaced in its web-search sources, but it still cannot guarantee an opening is current or that a person is the actual hiring manager. Direct full-JD retrieval, exact original post dates, poster identity and true hiring relationships often cannot be reliably extracted from public search snippets alone.
- Contact search provides potential leads and RocketReach enrichment when configured; two named people and exact emails cannot be guaranteed for each job.
- RocketReach API integration is unverified against your specific plan; the endpoint/parameters may need updating.
- Outlook/WhatsApp integrations and automatic applications on external hiring sites are not implemented.
- Resume tailoring is **conservative re-ranking of truthful bullets**, not deep AI rewriting; visual formatting differs from the uploaded master PDF.
- Hosted homepage is at `/` and safe browser-only preview is at `/preview.html`; the authenticated workspace is at `/app`. Production use requires Google OAuth, persistent PostgreSQL and real search API credentials.

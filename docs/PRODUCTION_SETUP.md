# Atlas production setup (Vercel)

This application is already deployed from the private GitHub repository.
Never paste API keys, OAuth client secrets, refresh tokens or database passwords into ChatGPT or GitHub.
Use **Vercel → analytics-job-agent → Settings → Environment Variables**, target **Production**.
After adding or editing environment variables, redeploy the latest GitHub commit.

## Configured by the assistant

- APP_BASE_URL = https://analytics-job-agent.vercel.app
- DEMO_MODE = false
- ALLOWED_GOOGLE_EMAIL = the email on the connected GitHub account
- SESSION_SECRET = cryptographically generated (encrypted Vercel variable)
- TOKEN_ENCRYPTION_KEY = cryptographically generated (encrypted Vercel variable)
- CRON_SECRET = cryptographically generated (encrypted Vercel variable)
- OPENAI_SEARCH_MODEL = gpt-6.1-sol
- OPENAI_MODEL = gpt-4.1-mini

Do not rotate TOKEN_ENCRYPTION_KEY after connecting Gmail without migrating encrypted refresh tokens.
No live sending or automatic follow-ups are enabled.

## 1. Google sign-in

1. Open https://console.cloud.google.com/auth/clients and create/select a Google Cloud project.
2. Configure Google Auth Platform: Branding (application name Atlas), Audience (External for a personal Gmail account), and add your own Google account as a test user when testing is required.
3. Create OAuth Client: **Web application**.
4. Authorized JavaScript origin: `https://analytics-job-agent.vercel.app`
5. Authorized redirect URI: `https://analytics-job-agent.vercel.app/api/auth/google/callback`
6. Copy the generated client ID and client secret directly into Vercel Production env vars `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` (type Encrypted).
7. Redeploy and visit `/api/auth/status`; `google_ready` should be true. Sign in using the configured owner account.

Google sign-in requests basic profile and email permissions only. Gmail access is requested separately via `/api/auth/gmail/start` after sign-in and database setup. Gmail API may need enabling and additional OAuth scopes/test-user consent. The Google Cloud project and consent configuration belong to the owner.

## 2. Persistent database

Use Neon Postgres (or any managed PostgreSQL provider). The Neon ChatGPT plugin may help create and manage a database after you connect it; alternatively install Neon from Vercel Marketplace/Storage.

- Connect a database to the Vercel project or copy the provider's **pooled PostgreSQL connection string** into `DATABASE_URL` as an encrypted Production variable.
- Do not expose `DATABASE_URL` in browser JavaScript.
- Confirm TLS is enabled in the provider connection settings.
- Redeploy. `/api/auth/status` should show `database_configured: true`.
- SQLAlchemy creates the initial tables when the backend first uses the database. Schema migrations should be added before substantial future changes.

## 3. OpenAI web-search API

1. Visit https://platform.openai.com/ and open a project dedicated to Atlas.
2. Configure billing and a low usage budget/alerts.
3. Go to https://platform.openai.com/api-keys and create a project-scoped secret key.
4. Add the secret as Vercel Production `OPENAI_API_KEY` with type Encrypted. Never add it to GitHub.
5. Keep `OPENAI_SEARCH_MODEL=gpt-6.1-sol` for independent web-search research, if available in your API project. See https://developers.openai.com/api/docs/guides/tools-web-search.
6. Redeploy, log in, then use **Find jobs with API + ChatGPT**. The ChatGPT engine runs independently even if Serper is not configured.
7. Source URLs are required; details such as actual posting date and hiring relationship may remain unverified. The app must not invent them.

ChatGPT Plus and OpenAI API billing are separate.

## 4. Optional second search engine and contact enrichment

- `SERPER_API_KEY`: Serper Google-index API for the independent indexed-search engine.
- `ROCKETREACH_API_KEY`: RocketReach professional contact lookup; validate against your account's API entitlement.
- Telegram requires `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` and `TELEGRAM_WEBHOOK_SECRET`.
- Gmail drafting requires Google OAuth with Gmail scopes and a persistent database.

## 5. Approval and email safety

- Job approval can create a tailored PDF from fact-locked `config/resume_facts.yaml`.
- The Gmail draft and scheduled email workflow is not ready until Gmail OAuth is connected and tested.
- `config/settings.yaml` has `scheduled_sending_enabled: false` and `followup.enabled: false`.
- Test one draft with your own email address before any live outreach.
- No unattended scheduler has been configured. The Vercel Hobby plan does not support reliable every-five-minute polling.

## Test checklist

- `https://analytics-job-agent.vercel.app/api/health` returns `ok: true`.
- `https://analytics-job-agent.vercel.app/api/auth/status` reports configuration readiness without exposing secrets.
- Sign in with Google and open the dashboard.
- Search one job and verify the original post URL before approving.
- Approve a test job and verify the draft is not sent immediately.

# Atlas audit and fixed resume decision — 2026-11-08

The owner's latest decision replaces all 80%-retention, one-page editing and project-selection requirements: use exactly two owner-supplied final PDFs, Analytics Professional and Data Science. Never generate or tailor a live attachment.

## Findings observed

- GitHub main at inspection: a9f2e602974dd257b7d6b6d5aba41d3f6a76e7c3.
- Primary production alias analytics-job-agent.vercel.app: deployment dpl_8dZq65iFYoznXDQxGqtVSXbBX8tu, READY, source redeploy, commit fc7421450b710ed30825319ac6c20e83f872251a. Rebuilding this old deployment did not update the code.
- GitHub main's primary Vercel status reported failure with a build-rate-limit URL. A second Vercel project reported success; this is not evidence that the primary alias is current.
- Public health returned 200, demo false. Authenticated browser loaded PostgreSQL-backed dashboard with three imported jobs and no outreach rows. Production lacked master upload UI.
- Vercel connector could read primary project/deployment summary, but deployment listing/protection operations returned 403. No authenticated CLI was installed. Browser remained usable.
- Original tests reused a shared temporary SQLite database and failed on repeated runs. Replaced with per-test disposable databases.
- Gmail attachment verification previously checked filename/count but not content. Now verifies SHA-256 of decoded attachment bytes.

## Changes

- Two private PDF slots under separate PostgreSQL KV keys; no automatic migration or guessing which old resume belongs to which slot.
- Owner upload, explicit replacement confirmation, original-byte preview/attachment, and hash-bound selection. Existing drafts keep their attached bytes.
- Invalid, encrypted and oversized PDFs rejected. No one-page restriction or PDF rewriting.
- Live custom resume routes removed; legacy self-test retired. Legacy generators remain only for existing demo/offline compatibility, not the live reviewed flow.
- Reviewed Gmail draft is still an explicit separate action, never sends/schedules. Job-row lock serializes concurrent draft submissions on PostgreSQL. Wrong attachments fail verification.
- Live rescheduling blocked. Health exposes deployment commit and fixed_pdf mode to make stale deployments detectable.
- No new database columns, secrets, production data changes, Gmail drafts or paid RocketReach lookups required for this change.

## Release and remaining checks

Run Python compile, JS syntax and complete pytest suite. Create one review branch/PR, then build the exact approved commit in primary Vercel project prj_G5rF7rl9Xb6EUc7WI6XXGyD50hGB (team_0ua9hFMi0PTjq0pVVzMYoqRM). Do not redeploy fc74214. Check alias and commit equality after release.

Owner must identify/upload the exact two final PDFs; similarly named historical resumes must not be silently selected. Live upload/preview and explicitly reviewed Gmail draft verification remain pending until those files and the new build are available. A Gemini UI smoke test was initiated but its transient result was not captured; no fresh success claim. RocketReach is configured, not freshly provider-tested. No claim of complete end-to-end production readiness.

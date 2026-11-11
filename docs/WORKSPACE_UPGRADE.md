# Atlas workspace upgrade

Compared against main commit 42ba298c9d06e3b85e859d17e6c4f2470ac50c64.

| Agreed requirement | Existing code | This change |
| --- | --- | --- |
| Excel job intake | Spreadsheet and CSV import, contact scope, batch progress | Preserved; records upload batch and filename in each job timeline |
| Faster data | Full-history overview and outreach dashboard reads | Paginated server reads: 20 jobs, 25 outreach rows or job logs; email body and resume PDF excluded from outreach list queries |
| Refresh | Manual dashboard reload; four-second import progress polling | Fifteen-second revision check while visible; reload only after revision changes; pauses while editing or in dialogs; import polling stops at completion |
| Clear outreach dashboard | Separate overlapping status and tracking tables | One searchable queue, status filters, reply action counters, expandable email and imported evidence, existing schedule/cancel controls |
| Separate logs dashboard | General activity feed and upload progress only | Read-only job milestones and per-contact chronological timelines; search, batch, IST date and event filters; timeline pagination |
| Accurate timing | Events plus reply detection timestamp | First upload, email availability, draft, send, reply detection; new Gmail events also store actual receipt time when available; absent history is “Not recorded” |
| Vercel scheduler | Schedule at :05/:20/:35/:50 Asia/Kolkata; separate import interval | Schedule and worker decisions preserved; UI describes dispatch cadence separately from import processing interval |
| Telegram alerts | Existing worker notifications | Preserved |
| Resumes and email prompt | Two fixed PDFs and configured drafting prompt | Preserved |

## Operational details

- Dashboard GET requests perform database reads only. They never enrich contacts, create Gmail drafts, send messages or invoke a language model. The existing job-detail enrichment recheck behavior remains on the imported-jobs view.
- The old full overview API is retained for compatibility, while the upgraded UI uses section-specific reads.
- Detail expansion loads one email and its imported job evidence. The list never returns resume attachment data or email body text.
- Log dates filter event occurrence in the selected inclusive IST calendar range. Milestone columns show the first recorded event across the whole job history, rather than only events within that filter.
- A spreadsheet profile-only update is not treated as completed email enrichment. New supplied-email imports and successful RocketReach results emit explicit email-availability events.
- No historical event times are reconstructed from job creation dates. Existing reply detection timestamps are retained; Gmail receipt time is separate.
- No schema migration, cron registration, provider configuration or production data rewrite is required.

## Validation

- New Python tests cover bounded reads, exclusion of PDF/body columns, first-event selection, missing history, batch/event/date filtering, contact attribution, receipt versus detection metadata, revision changes and endpoint authorization.
- Node tests cover existing resume review controls plus workspace navigation, missing-history rendering, unchanged-revision refresh and GET-only dashboard loads.
- The untouched baseline and modified application both have the same seven pre-existing Python failures (three Gemini expectations, MIME job-link expectation, two holiday scheduling expectations, one omitted-contact warning expectation). These remain outside this dashboard change.
- A connected Chrome browser could not reach the container's localhost server. Browser visual checks and production latency are therefore not verified; no claim of subsecond production load is made.

## Next increments

1. Validate the new workspace visually in a reachable preview and measure authenticated production read latency before choosing further caching or indexes.
2. Improve email generation observability and provider failure recovery using the configured prompt and verified candidate facts; evaluate fallback models explicitly rather than selecting an unconfigured model.
3. Expand follow-up management and response reporting while retaining working-day scheduling and Telegram notifications.

The reference application's visible UI informed the navigation and queue layout. Its private backend architecture was not observable and is not assumed.

# Dashboard read performance

Profiling against commit e67dcb7 identified repeated database round trips and redundant browser requests.

- A warmed `/api/overview?section=outbox` performed 26 SQL statements, including two reads of full base64 resume PDFs. A reproducible offline profile with both legacy PDF uploads now performs six statements and zero PDF reads after one-time metadata backfill. This measures database request count, not production wall-clock latency.
- Dashboard counters now use one SQL statement instead of nine database round trips.
- Scheduler/settings key-values are fetched together and cached only within that database session. There is no shared cross-request cache of private settings or credentials.
- Resume listing uses separate metadata rows. New uploads update metadata in the same transaction. Legacy uploads receive conflict-safe metadata backfill on first listing. Actual PDF preview, attachment creation and integrity checks still read and verify original bytes.
- Job lists already contain their details. The browser no longer fetches the selected job again after loading the list, and selecting another job on the same page renders locally without reloading the list.
- Visited tabs render from in-memory view snapshots immediately, then revalidate. Navigation reuses workspace context for up to 30 seconds; explicit refreshes and revision-driven background refreshes request fresh context. Draft/send actions retain server-side validation and never rely on cached UI data for authorization.
- Navigation cancels superseded read requests. Searches and actual mutations keep their existing behavior.
- Authenticated API responses include `Server-Timing: app;dur=...` to distinguish application processing from network/cold-start latency.

The Vercel scheduler, Telegram notifications, Excel workflow, selected PDFs and controlled-email validation are unchanged. No new database schema is needed. Metadata keys are additive; resume bytes are not moved or modified.

Tests assert one-round-trip counters, bounded overview query count, no attachment-blob reads during dashboard load, metadata synchronization after resume replacement and immediate cached rendering before a network response. Production cold starts, network routing and database wake-up can still affect first-load latency; this change does not claim a guaranteed subsecond first load.

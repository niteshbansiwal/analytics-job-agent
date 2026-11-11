# Controlled email drafting

The Email Studio provides one to four subject templates, stable rotation or a fixed subject, an editable first-email template, enabled candidate achievements, a default fixed resume PDF and zero to two LLM repair attempts.

## Flow

1. Resolve the imported job, selected contact and selected uploaded resume before generating. A reviewed preview requires a real contact name, a detailed JD and an original HTTPS posting URL. Automated Excel processing uses the default resume in Email Studio.
2. Ask the model for exactly `value_proposition` and two distinct `fact_ids` from enabled candidate facts. The saved style prompt and actual JD are included. The contact name, email and resume PDF bytes are not sent to the model. The selected resume profile is identified by its Analytics/Data Science label.
3. Validate JSON fields, types, length, placeholder text, fact IDs and paragraph content. Numeric claims belong in the fixed achievements, not model-written copy. Model output with missing or malformed fields triggers a corrective prompt containing the specific validation error, up to the saved repair limit. Provider quota/network failures are surfaced rather than repeatedly called. Exhausted repairs hold the contact for review.
4. Render the subject and body locally. Atlas fills the actual first name, company, job title, exact source URL and two unchanged candidate achievements. Subject selection is deterministic per posting URL and contact, so regeneration does not randomly switch lines. Subjects are limited to the email header length.
5. Show the rendered email, chosen contact, selected fixed PDF and validation/repair result. Changing contact or resume restarts generation; the review selections are locked to prevent a contact swap with old personalization. Owner edits to subject/body are validated again before Gmail creation.
6. Generate multipart email with the URL in plain text and an escaped clickable anchor in HTML. No model-authored HTML executes. Verify the returned Gmail recipient, subject, body, job hyperlink and exact attachment hash before accepting the draft.
7. At scheduled sending, inspect the actual current Gmail draft. Reject variables, the wrong greeting/contact, missing job hyperlink, changed recipient or incorrect resume. Re-read immediately before the send API and reject changes since validation. Validation failures before a send attempt become `needs_attention`; ambiguous provider-send failures remain `sending_review` and never auto-retry.

The send API cannot provide an atomic compare-and-send operation; an edit made outside Atlas after its final read is subject to Gmail's API behavior. These guards cover Atlas-controlled creation and sending. They cannot prevent a person sending independently from Gmail.

## Resume and factual controls

Atlas attaches one of the two original uploaded PDFs unchanged. Enabled facts are the saved candidate facts already present in Atlas; owners should enable only facts supported by their chosen resume. The model selects fact IDs and cannot rewrite the bullet claims. This is not an automatic semantic certification that every saved fact appears in both PDFs. PDF byte identity is verified separately.

## Compatibility and operations

- Excel intake, job discovery policy, Vercel schedule, holiday rules, Telegram behavior and follow-up enablement are retained.
- Saving templates affects new generations; it does not rewrite, delete, resend or repair existing Gmail messages.
- Existing scheduled drafts pass through the new sending checks. Invalid ones are held for review, not automatically regenerated and sent.
- Follow-ups retain their existing generation logic. They also pass variable/header checks, recipient checks and the final draft re-read. The initial-email greeting, job-link and resume checks do not force additional attachments on a follow-up.
- Preview/draft events record repair count, template subject index, selected resume and prompt fingerprint without logging full model prompts or email bodies in Job Logs.
- No database schema migration is required; controls use the existing key/value store.

## Validation

Offline tests cover placeholder syntaxes in both fields, bounded repair, malformed or extra JSON fields, unknown facts, numeric claims, contact binding, resume selection, URL hyperlinks (including query parameters), HTML escaping, API configuration validation, manual Gmail edits, send refusal and the valid scheduler send path. Node contracts cover the new studio and formatted preview.

Three unrelated existing failures remain: two holiday scheduling expectations and one spreadsheet omitted-contact warning expectation. These failures were present before this work. Previous email number/URL tests now pass after fixing escaped regexes and the hyperlink renderer. Legacy free-form email tests have been replaced by the structured output contract tests.

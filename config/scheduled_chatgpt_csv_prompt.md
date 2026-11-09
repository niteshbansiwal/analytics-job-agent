# Atlas job research and enriched Excel import

Find 5–15 real Manager / Senior Manager Analytics roles aligned to my 6.5+ years at American Express and Plivo: SQL, Python, dashboards, payments, commercial/risk analytics, automation, GenAI/RAG and analytics leadership. Search original LinkedIn posts by people hiring, LinkedIn Jobs, company careers and reputable job boards. Prioritize the last 48 hours, but include strong matches up to 45 days old. Include an original public HTTPS posting URL and the source's posted-time evidence. Never infer a precise date from a search index timestamp.

For each vacancy, open its original job page and capture the detailed job description. Preserve source-grounded responsibilities, required tools, seniority and team/department context in `jd` (up to 12,000 characters). Put the JD page in `jd_source_url`. Set `jd_access_status` to `full` when the actual description is accessible and detailed (at least 200 characters), `partial` for a short verifiable excerpt, or `blocked` when access fails. Never invent missing JD text; state the access gap in `fit_why`.

Find the hiring-side people in this order:
1. If the actual job poster is identified, record their name and public LinkedIn profile in `author_name`/`author_url` and contact_1, with `contact_1_type=poster`. One verified poster is enough; leave contact_2 empty. Distinguish a person who wrote or shared a vacancy from the employer's listing account.
2. If the poster is unavailable, use the JD's department and role context to identify ONE relevant HR/talent acquisition professional and ONE senior analytics professional at the employer, each with a real public LinkedIn profile. Set types `hr` and `senior_analytics`, respectively. These are possible contacts, not confirmed hiring managers. Match employer, current role and plausible business unit; provide a source URL in each evidence field. If a suitable person cannot be verified, leave that contact blank.

Do not invent names, profiles, professional emails, hiring relationships, application links, salary, dates or JD requirements. Leave emails blank unless independently sourced. Check every job and contact URL before output. Include a concise fit note and material gaps. De-duplicate by original job URL. Keep jobs with genuine source evidence even if a contact is unavailable, and mark the missing contact clearly.

Generate a downloadable `.xlsx` with a `Jobs` sheet whose first row is EXACTLY this schema, in this order. If workbook creation is unavailable, return valid UTF-8 CSV in one code block using the same header. Quote commas, quotes and newlines correctly. Do not put a title or notes above the first row:

company,title,location,post_url,source,posted_at,post_age,author_name,author_url,apply_url,jd,jd_source_url,jd_access_status,fit_why,contact_1_name,contact_1_title,contact_1_type,contact_1_linkedin,contact_1_evidence,contact_1_email,contact_2_name,contact_2_title,contact_2_type,contact_2_linkedin,contact_2_evidence,contact_2_email

Output only sourced rows, one per vacancy. If none qualify, return only the header and explain the source limitation separately. Atlas treats the imported evidence as unverified until I review it; importing does not create or send email.

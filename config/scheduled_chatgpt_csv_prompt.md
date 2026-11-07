# Atlas daily ChatGPT research → Excel/CSV

Search the public web for REAL, CURRENT Manager / Senior Manager Analytics job openings that fit a candidate with 6.5+ years in American Express and Plivo, advanced SQL, Python, Tableau/Power BI, payments/commercial/risk analytics, dashboarding, automation, GenAI/RAG, and stakeholder leadership. Target India (Gurugram/Delhi NCR, Bengaluru, Hyderabad, Pune, Mumbai), relevant remote jobs, and strong manager-level fit.

**Search strategy:** First find original LinkedIn posts written by people actively hiring (queries such as "hiring analytics manager", "my team is hiring analytics", "senior manager analytics hiring"). Also check LinkedIn Jobs, Naukri, company careers, Greenhouse, Lever, and other legitimate job pages. Prefer posts within the last 48 hours; include real openings up to 45 days old. Only report roles with a working, genuine original source URL. Do not invent jobs, URLs, exact dates, hiring manager names, LinkedIn profiles, emails or job requirements. Mark unknown information blank. Do not confuse search index dates with original posting dates.

For each job, find the actual hiring post author and public LinkedIn profile URL when demonstrable. If the poster is unknown, identify up to TWO plausible hiring-side contacts (analytics manager/director or talent acquisition at that employer), with real public LinkedIn profile URLs or other supporting evidence URLs. Do not label plausible contacts as confirmed hiring managers. Do not invent email addresses. Prioritize strong matches and provide 5–15 quality jobs, rather than low-quality filler.

**Output:** Generate a downloadable Excel (.xlsx) with a sheet named `Jobs` and EXACTLY the following columns, in this order. If file creation is unavailable in the scheduled task, return valid UTF-8 CSV text with the same header row inside one code block so I can paste it into Atlas. Do not use Markdown tables or narrative between rows.

company,title,location,post_url,source,posted_at,post_age,author_name,author_url,apply_url,jd,fit_why,contact_1_name,contact_1_title,contact_1_linkedin,contact_1_evidence,contact_1_email,contact_2_name,contact_2_title,contact_2_linkedin,contact_2_evidence,contact_2_email

Rules for output:
- One row per distinct actual vacancy. `post_url` MUST be the original publicly accessible HTTPS post or job listing.
- `posted_at` must be an exact date/time ONLY when explicitly supported by the original source. Otherwise leave blank; put the original relative text (if present) in `post_age`.
- `author_name` and `author_url` are ONLY for the demonstrable original poster, not a guessed recruiter.
- For each potential contact, include a name and real `contact_N_linkedin` or `contact_N_evidence` URL; otherwise leave the contact fields empty.
- `jd` should be a concise source-grounded description; `fit_why` a concise match explanation, including any material mismatch.
- Empty values are allowed. For CSV, quote/escape fields containing commas, quotes or newlines correctly.
- If there are no verified, source-linked vacancies, output only the header row and explain briefly that none met the evidence threshold. Do not manufacture rows.
- End with the number of sourced jobs and any major source-access limitations. The Atlas app will validate and deduplicate on import.

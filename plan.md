# CANDIDATE OUTREACH AUTOMATION — BUILD SPEC

**INPUT (per candidate):** resume (docx, required); current employer (required — used to exclude from outreach targets); optional LinkedIn profile, career goals, culture preferences, target industry/role.

**TRIGGER / RUN MODE:**
- V1: manual — candidate explicitly approves/triggers a run (no background automation).
- V2: candidate can opt into "daily auto-run," which triggers the pipeline automatically each day until the candidate disables it (e.g., once placed or no longer searching).

## PIPELINE

**1. Company Discovery**
- V1: LLM-only. LLM identifies target companies matching candidate profile + goals (career goals, culture preferences, target industry/role) via reasoning + web search.
- Exclude the candidate's current employer from consideration — LLM is instructed not to target it.
- V2 (possible): use Hunter Discover/Discover People as a free pre-filter on the LLM's shortlist, to check email-data availability per company before committing research effort in later steps.
- Exclude a contact if they appear in the candidate's Sent List within the retention window (see SENT LIST below). "Same company, different person" is never blocked — just deprioritized toward the backup contact.
- Note: different candidates may independently be pointed at the same contact — allowed by design, since each candidate's resume/content differs. No cross-candidate coordination needed.
- Output: list of companies (name, domain).
- **Candidate Review Gate:** before Contact Identification proceeds, present the shortlist to the candidate. Candidate can deselect any companies to exclude them from research. Only approved companies continue to step 2.

**2. Contact Identification (primary + backup, both upfront, per company)**
- Identify 2 contacts per company: primary + backup.
- For each: verify still employed there using dated evidence (recent post, updated title, sourced date) — do not accept an inferred/unconfirmed answer.
- Output per contact: name, title, company, LinkedIn URL, employment_verified (bool + evidence snippet).

**3. Email Lookup (sequential, gated — not parallel with step 4)**
- Call Hunter email_finder(domain, first_name, last_name) on primary contact (see hunter_client.py).
- If valid email returned -> pass this contact to step 4.
- Else if backup contact employment_verified=True -> call email_finder on backup.
- Else -> drop this company from the pipeline (no valid email = no outreach).

**4. Personalization Research (only for the one contact with a confirmed email)**
- Gather recent posts/articles/projects for personalization.
- Red Flag Detection (opt-in setting, default off): if enabled, also screen for scam signals and concerning social media indicators. If any surface, show them to the candidate alongside the draft rather than auto-skipping the contact — candidate decides whether to proceed.
  - Note: red flags found here happen after the Hunter email lookup already ran, so if the candidate declines to reach out, that Hunter cost (~2-3 cents) goes unused. Accepted tradeoff for simplicity.
- V2, opt-in only: prompt candidate to optionally upload the contact's LinkedIn profile (PDF export) to improve research quality.

**5. Generate Outreach**
- LLM drafts personalized email (subject + body) using: candidate profile/goals + contact info + step 4 research.
- All outreach emails include standard compliance/consent boilerplate (clear sender identification, unsubscribe/opt-out instructions) — included by default, not editable/removable by the candidate.
- UI: draft is presented for read-through; one-click copy button. Reading before copying is the only approval step — no separate approval workflow.
- Add contact to candidate's Sent List automatically upon draft generation (date_sent = generation date). Candidate can manually remove or edit any entry.
- Sent List reconciliation: next time the candidate opens the app, nudge them to confirm whether they actually sent to companies added since their last visit ("Did you send to these?"). Sent List is manually editable at any time regardless of the nudge.
- UI: simple "Interview Arranged" checkbox per Sent List entry, toggled manually by the candidate — the only outcome-tracking signal for v1. Full response tracking deferred to a future version.

**6. V2 Extension:** tailor candidate's resume text per contact/company.

**7. Worth considering (V2):** auto-create the drafted email as a Gmail/Outlook draft (OAuth scoped to drafts-only — never send automatically).

## RUN REPORTING (basic)
- At the end of each run, present a summary to the candidate: companies attempted, companies dropped (with reason — no valid email / employment unverified / excluded by candidate), contacts found, drafts generated, entries pending send confirmation.
- For V2 daily auto-run: candidate is notified when a new run completes with drafts ready, so results don't silently pile up unseen.

## SENT LIST (per candidate, CSV)
Columns: candidate_id, company_domain, contact_name, contact_email, date_sent, interview_arranged (bool, manually set)
- Exclusion rule: skip a contact if (today - date_sent) < RETENTION_MONTHS for that exact contact_email.
- RETENTION_MONTHS: configurable, default 12 (range 6-18).
- Expired entries no longer block outreach and can be pruned periodically.
- Entries are writable/editable by the candidate at any time (see reconciliation nudge in step 5).

## CONCURRENCY
- Steps 2->5 run sequentially within a single company's pipeline (email lookup gates research; research gates generation).
- Multiple companies' pipelines run concurrently via async.
- Cap: 30 companies per candidate run (10 for beta).
- Separately cap concurrent Hunter API calls (e.g., semaphore of 10) to respect Hunter rate limits regardless of company-level concurrency.

## CACHING (V2)
- Not needed for v1 rate-limiting (the Hunter concurrency semaphore already handles that) — this is a cost/latency optimization for later.
- Key by company domain + contact name/LinkedIn URL, to avoid repeat Hunter calls and repeat research across candidates/runs.

## TECH STACK
- Python (FastAPI), async/await for concurrency across companies.
- HTMX/pico.css for prototype UI; keep pipeline logic decoupled from UI so it can be swapped for a web UI later.
- hunter_client.py: existing Hunter API wrapper (account info, domain search, email finder, email verifier, enrichment, discover, leads).

## OUTPUT
Per company: contact used (name, title, LinkedIn URL), verified-employment evidence, email address, personalization research summary, red flags (if enabled), drafted outreach email (subject + body), interview_arranged status.

## OUT OF
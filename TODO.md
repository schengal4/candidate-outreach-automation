# TODO — Candidate Outreach Automation

Handoff notes for continuing this project in a new chat. Current state: V1 pipeline
is working end-to-end (discovery → review gate → sequential contact/backup →
Hunter → research → draft), Gmail draft integration is live (OAuth, PKCE, manual
"Save to Gmail" per draft), and the draft-writing prompt has been tuned against
real output (anti-AI-isms rules, one-opener/one-accomplishment focus, plain
signature instead of a compliance footer). See `README.md` for full architecture
and setup.

## Done (July 2026)

3. **Sent List: permanent exclusion flag** — implemented (was the real gap
   from the compliance review: the retention-window block was time-limited
   only). A `permanently_excluded` boolean per Sent List entry — "Never
   contact" toggle in the table (like "Interview Arranged", with a confirm
   dialog when turning it on) plus a checkbox on the manual-add form. Blocks
   outreach forever regardless of `date_sent` (`_is_active` returns True), so
   it also survives "prune expired" — the only ways to lift it are toggling it
   off or deleting the entry (delete warns when the entry is a never-contact
   one). Older CSVs without the column load as not-excluded.

1. **Resume attachment on Gmail drafts** — implemented. `.docx` → PDF via
   installed MS Word COM (`app/pdf_convert.py`, pywin32; LibreOffice wasn't
   installed on the host, Word was). Converted once at upload time and cached
   next to the resume file; `ensure_resume_pdf()` also converts lazily for
   candidates created before the feature. `_build_raw_message()` now builds
   multipart/mixed with the PDF attached under the uploaded filename (as .pdf,
   candidate-id prefix stripped). Draft creation fails loudly if conversion
   fails, since `DRAFT_SYSTEM` writes bodies that reference the attachment.

2. **Sent List: manual "Add" entry** — implemented. Form at the bottom of the
   Sent List page + `POST /sent/{candidate_id}/add`. Manual entries get
   `confirmed_sent=True` (they were sent outside the app — no reconciliation
   nudge) and an optional past `date_sent` (defaults to today).

4. **"Save all to Gmail" batch button** — implemented.
   `POST /runs/{run_id}/save_all_to_gmail` + a button at the *bottom* of the
   finished run report (after all drafts have been on screen), shown only when
   2+ drafts are unsaved. Per-draft failures don't stop the rest; errors show
   next to each draft. The per-draft buttons and the human review checkpoint
   are unchanged (kept deliberately — reviewing a draft's factual claims
   against real evidence, like the Idan Bassuk "talk" claim during the
   original build, has already caught a real hallucination-adjacent issue).

## Deferred / future

5. **Auth** — implemented (July 2026), following the original design note:
   Google Sign-In (`openid email profile` — separate, narrower flow from
   `gmail.compose`, same OAuth client), not a password table. `app/auth.py` +
   a login-wall middleware in `main.py`; signed session cookies (14 days,
   secret persisted in `data/session_secret`); optional
   `ALLOWED_LOGIN_EMAILS` allowlist on top of Google's Testing-status test-user
   gate; `REQUIRE_LOGIN=0` opts back out. Requires
   `http://localhost:8000/auth/callback` as a second Authorized redirect URI
   in the Cloud Console. Identity and draft-destination remain separable —
   Outlook drafts (item 6) wouldn't touch the login flow.
   Per-account data isolation included: candidates carry an `owner_email`
   (stamped at creation from the session), the home page filters to the
   logged-in account, and every candidate-scoped route (candidate page, runs,
   sent list, Gmail) 404s for non-owners — indistinguishable from not-found.
   Existing candidates were migrated to venkatachengalvala@gmail.com. In open
   mode (`REQUIRE_LOGIN=0`) no filtering applies, matching the original
   single-user behavior.

6. **Outlook draft integration** — explicitly out of scope for now. Gmail
   integration was kept in its own `gmail_client.py` rather than forced into a
   generic multi-provider abstraction, so Outlook should be a parallel module
   later, not a refactor of the existing one.

## Longer-tail backlog (from the original build spec, still V2)

- Daily auto-run (candidate opts in, pipeline runs automatically until disabled)
- Hunter Discover/Discover People as a free pre-filter before full research
- Candidate can optionally upload a contact's LinkedIn PDF export to improve
  personalization research
- Per-company resume tailoring (rewrite resume text per contact/company)
- Caching keyed by company domain + contact — avoid repeat Hunter calls and
  repeat research across candidates/runs (cost/latency optimization, not
  needed for correctness)

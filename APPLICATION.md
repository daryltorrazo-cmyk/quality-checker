# Credit Quality Checker

Module 2 is a local development application for inquiry screening, positive-account protection, documented QA decisions, and document/letter cross-checks. The CreditPro dashboard includes a dark sidebar, upload cards, highlighted letter preview, grouped issue panel, bureau filters, inquiry table, and persistent audit trail. It uses Python 3.12+ and the pinned `pypdf` dependency for PDF extraction; Word extraction uses the standard library.

## Run and test

```sh
cd /workspace/quality-checker
python3 -m venv .local/venv
.local/venv/bin/python -m pip install -r requirements.txt
.local/venv/bin/python -m unittest discover -s tests -v
.local/venv/bin/python -m credit_checker.server
```

The server binds to loopback port 8000 (override with `PORT`). Its dashboard is served at `/`; `/health` is available for local readiness checks. With no identities configured, the dashboard loads but financial-data APIs deny access.

For authenticated development, set `CQC_IDENTITIES` to a JSON object mapping individual reviewer names to `{ "role": "agent" | "qa" | "admin", "token": "…" }`. Supply your own distinct tokens of at least 16 characters through your local environment or a private configuration file. Never commit or share tokens. Enter your individual token in the dashboard; it is held only in memory. Roles are enforced server-side, including reviewer attribution. Administrators maintain relationships; QA reviewers make inquiry and final-letter decisions; agents import and screen reports. An administrator token does not grant QA privileges. This is a local development identity mechanism; production requires an identity provider, HTTPS, tenant isolation, encrypted storage, retention policy, and operational audit controls before handling real client data.

The default SQLite database is `.local/checker.sqlite3` (ignored by Git, file mode 0600); override with `CQC_DB`. Original uploaded file bytes, extracted text, source reports, registry records, review evidence, approved letters/schedules, and historical audit events persist across restarts. These files contain sensitive data; use illustrative data for development.

## Workflow

1. Connect through Settings or the profile button. Upload a source credit report as PDF, Word (`.docx`), TXT, CSV, or JSON (10 MB maximum). The extraction dialog shows original text and editable inquiry/account records. Add or correct any records not extracted, and verify every inquiry and every open/positive account against the complete original report. No missing bureau or creditor affiliation is invented. Dates use `YYYY-MM-DD`; bureau codes are `TU`, `EX`, and `EQ`. Reimporting a check ID replaces its source report and invalidates changed reviews. The dashboard also offers clearly marked illustrative data.
2. Review the summary, bureau tables, original identities, possible account connections, evidence, and duplicate flags. Every inquiry is compared against every open account and every closed account with positive history across all bureaus. No inquiries are merged or deleted.
3. Administrators add documented aliases, parent/subsidiary affiliations, issuers, dealers, financing partners, fintech partners, servicers, mortgage brokers, or historical relationships. Records have optional product lists, validity dates, and bank/partner fields. No real affiliations are seeded. Unverified relationships remain suggestions. Historical relationships require dates covering both inquiry and account opening. Changes invalidate previous QA decisions.
4. QA selects “View details” and completes the review form. All eight checklist fields must be checked. Document evidence, authorization and permissible-purpose findings, application findings, date findings, affiliation findings, auto-financing findings, and individual findings for **every** protected account. For a protected decision, identify the confirmed positive account; the result becomes **MATCH — PROTECTED**. For eligibility, supply a specific factual dispute basis. Existing confirmed protected decisions cannot be changed directly to eligible. Evidence must establish the findings; a checklist attestation does not make unsupported facts true.
5. Upload the final dispute letter as PDF, Word (`.docx`), TXT, or CSV. Verify extracted inquiry entries against its full original text in the review dialog. Run Cross-Check compares the exact bureau, name, and date against source and QA results. Protected inquiries, unresolved reviews, absent inquiries, transcription errors, and duplicates block approval. Original text remains visible with problematic entries highlighted, and View Original downloads the preserved file. No entries are silently removed; submit an updated final letter after fixing issues. A CSV schedule can also be pasted in the advanced editor.
6. A QA reviewer must separately verify the complete original letter, confirm extraction completeness, and document final approval evidence. Only then can it become **READY TO SEND**. The approval binds the exact original file hash, extracted inquiry schedule, source report/relationship fingerprint, and current QA decisions. Changes require fresh cross-checking and review. Approval retrieval (`GET /api/letter/<letter_hash>`) revalidates against current data and returns **BLOCKED** if stale. There is no sending integration.

### Document extraction

Text-based PDFs (up to 100 pages), DOCX body paragraphs/tables/headers/footers, and UTF-8 text files are supported. Legacy `.doc` files must be converted to DOCX. Scanned PDFs and pages without extractable text are flagged and **block approval**, even if a schedule is entered manually; perform OCR externally and upload a fully readable replacement. The app has no OCR service.

Inquiry detection recognizes CSV schedules, explicit pipe-separated records, bureau headings, and lines such as `1. SOME BANK (Inquiry dated 05/10/2026)` under an `Experian` heading, or `EX — SOME BANK — 2026-05-10`. It never assigns a missing bureau. Ambiguous layouts and lines are flagged for manual verification. Account extraction from arbitrary credit-report layouts is intentionally conservative: structured JSON, typed CSV, or explicit account records are recognized; add any missing accounts in the extraction dialog. Automatic text extraction is **not** a guarantee of document completeness. Human verification is mandatory.

Example TXT source records:

```text
INQUIRY | TU | ILLUSTRATIVE CREDITOR | 2026-05-10
ACCOUNT | EQ | ILLUSTRATIVE BANK | 2026-05-12 | open | positive | credit_card
```

Typed source CSV may use columns `row_type,id,bureau,name,date,opened,status,payment_history,product`, with `row_type` set to `inquiry` or `account`. A source CSV containing only `bureau,name,date` imports inquiries but warns that open/positive accounts must be added and verified.

### Report format

```json
{
  "inquiries": [{"id":"i1","bureau":"TU","name":"ILLUSTRATIVE CREDITOR","date":"2026-05-10"}],
  "accounts": [{"id":"a1","bureau":"EQ","name":"ILLUSTRATIVE BANK","status":"open","payment_history":"positive","product":"credit_card","opened":"2026-05-12"}]
}
```

Account `status` is `open` or `closed`; `payment_history` is `positive`, `negative`, or `unknown`. `opened` may be omitted if unavailable, which conservatively triggers investigation for otherwise unconnected pairs. Additional source metadata is retained. Products may describe credit cards, secured cards, auto loans, mortgages, personal loans, student loans, credit-builder loans, or other credit products.

### Letter schedule format

```csv
bureau,name,date
TU,ILLUSTRATIVE CREDITOR,2026-05-10
```

Creditor names must match the original source exactly; use CSV quoting for names containing commas. For an uploaded letter, the verified schedule remains attached to its immutable file hash. When a schedule is pasted without an uploaded document, approval covers that schedule only. The app does not automatically verify unstructured legal prose or personal identifiers (address, DOB, SSN); those require separate review.

## Protection policy and limits

Identical names and verified affiliations produce **RELATED — PROTECTED PENDING REVIEW**, because they do not establish the same transaction. Similar names, unverified affiliations, missing dates, and date proximity (7 days before through 90 days after an inquiry) produce **CHECK — MANUAL INVESTIGATION**, a protection hold. These heuristics are investigation suggestions, not AI or verified facts. No external AI service is used. Accounts far outside that window may still be related to refinancing or limit increases; mandatory manual QA account-by-account findings and application review remain necessary before eligibility.

No supported candidate produces **NO MATCH FOUND — AUTHORIZATION REVIEW**, never automatic eligibility. Only documented QA decisions produce **DISPUTE ELIGIBLE — QA VERIFIED**. A confirmed match is always protected. There is no automatic dispute selection. Disputing an inquiry does not automatically delete an associated account.

Duplicate source rows remain visible and block final approval until the source is corrected using verified information; the application does not remove them automatically. Company relationship evidence is recorded for human verification; URLs are not independently authenticated or researched by this application. Registry completeness and report completeness must be verified by the reviewer; the program cannot establish that an omitted creditor or source page does not exist.

## API

All `/api/*` routes require an individual token in `X-CQC-Token` (legacy bearer tokens are also accepted). The separate header lets Cloud Run keep its Google IAM authorization token. Use `POST /api/report` with `{id, report}`, `GET /api/report/<id>`, `GET /api/relationships`, administrator `POST /api/relationship` with `{record, verify}`, QA `POST /api/review` with `{report_id, inquiry_id, review}`, and `POST /api/letter/check` with `{report_id, text}`. Final QA `POST /api/letter/approve` additionally requires the latest `fingerprint`, `letter_hash`, and `approval_evidence`. Administrator `GET /api/audit` exposes historical changes. No API trusts a client-provided reviewer identity or role.

`POST /api/document` accepts `{filename, kind: "source" | "letter", content_base64}` and returns original extracted text, draft records, warnings, and a document ID/hash. Authenticated `GET /api/document/<id>` returns the preserved original file and extraction. For document-backed report imports, include `document_id` and `source_verified: true`; unreadable or unverified sources are rejected. For letter checks and approvals include the uploaded `document_id`; QA approval additionally requires `document_verified: true`. `GET /api/activity/<report_id>` returns saved check activity without full audit evidence and is available to all authenticated roles.

Tests cover cross-bureau screening, closed positive accounts, conservative date and name matching, alias/historical validity, QA evidence requirements, protected-account non-override, approval invalidation, duplicates, transcription errors, authentication, role permissions, real PDF/DOCX extraction, unreadable-page blocking, malformed uploads, original-file retention, extraction confirmations, and the live HTTP document approval flow.

## Google Cloud Run

See [the isolated deployment plan](deploy/README.md). Cloud actions require explicit approval. This project uses only the new quality-checker repository and new Quality Checker Google Cloud project.

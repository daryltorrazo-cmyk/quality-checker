"""Conservative screening: names and timing never establish a transaction."""
import csv
import hashlib
import io
import json
import re
from datetime import date
from difflib import SequenceMatcher

BUREAUS = ("TU", "EX", "EQ")
MATCH = "MATCH — PROTECTED"
RELATED = "RELATED — PROTECTED PENDING REVIEW"
CHECK = "CHECK — MANUAL INVESTIGATION"
NONE = "NO MATCH FOUND — AUTHORIZATION REVIEW"
ELIGIBLE = "DISPUTE ELIGIBLE — QA VERIFIED"
CHECKLIST = ("all_accounts", "affiliations", "auto_financing", "dates",
             "application_connection", "authorization", "factual_basis", "qa_approval")
RELATIONSHIP_TYPES = ("alias", "parent", "subsidiary", "issuer", "financing_partner",
                      "dealer", "servicer", "fintech_partner", "historical", "mortgage_broker")


def normalized(value):
    return re.sub(r"[^A-Z0-9]", "", value.upper())


def required_text(obj, key):
    value = obj.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be nonempty text")
    return value


def exact_date(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("Dates must use exact YYYY-MM-DD format")
    return date.fromisoformat(value)


def validate_report(report):
    for collection in ("inquiries", "accounts"):
        if not isinstance(report.get(collection), list):
            raise ValueError(f"{collection} must be a list")
        if len(report[collection]) > 1000:
            raise ValueError("Maximum 1000 entries per collection")
        ids = set()
        for item in report[collection]:
            for field in ("id", "name", "bureau"):
                required_text(item, field)
            if not normalized(item["name"]):
                raise ValueError("Creditor name must contain letters or numbers")
            if item["id"] in ids:
                raise ValueError(f"Duplicate {collection} ID")
            ids.add(item["id"])
            if item["bureau"] not in BUREAUS:
                raise ValueError("Bureau must be TU, EX, or EQ")
            if collection == "inquiries":
                exact_date(item.get("date"))
            else:
                if item.get("status") not in ("open", "closed"):
                    raise ValueError("Account status must be open or closed")
                if item.get("payment_history") not in ("positive", "negative", "unknown"):
                    raise ValueError("Account payment_history must be positive, negative, or unknown")
                required_text(item, "product")
                if item.get("opened"):
                    exact_date(item["opened"])
    return report


def validate_relationship(record, verify=False):
    for key in ("id", "inquiry_company", "official_creditor", "relationship_type"):
        required_text(record, key)
    if record["relationship_type"] not in RELATIONSHIP_TYPES:
        raise ValueError("Unsupported relationship type")
    for key in ("known_variations", "related_products"):
        if not isinstance(record.get(key, []), list) or any(not isinstance(x, str) or not x.strip() for x in record.get(key, [])):
            raise ValueError(f"{key} must be a list of nonempty strings")
    for key in ("parent_or_subsidiary", "issuing_bank", "financing_partner", "evidence_source"):
        if key in record and not isinstance(record[key], str):
            raise ValueError(f"{key} must be text")
    for key in ("valid_from", "valid_to"):
        if record.get(key):
            exact_date(record[key])
    if record.get("valid_from") and record.get("valid_to") and record["valid_from"] > record["valid_to"]:
        raise ValueError("Invalid relationship validity interval")
    if verify:
        required_text(record, "evidence_source")
        exact_date(record.get("last_reviewed"))
        required_text(record, "verified_by")
    return record


def fingerprint(report, relationships):
    return hashlib.sha256(json.dumps([report, relationships], sort_keys=True).encode()).hexdigest()


def pair(inquiry, account, relationships):
    reasons = []
    verified = False
    iname, aname = normalized(inquiry["name"]), normalized(account["name"])
    if iname == aname:
        verified = True
        reasons.append({"type": "direct_name", "evidence": "Identical normalized creditor names; transaction unconfirmed", "verified_fact": True})
    for rel in relationships:
        left = [rel["inquiry_company"], *rel.get("known_variations", [])]
        right = [rel["official_creditor"], rel.get("issuing_bank", ""), rel.get("financing_partner", ""), rel.get("parent_or_subsidiary", "")]
        if iname not in [normalized(x) for x in left] or aname not in [normalized(x) for x in right if x]:
            continue
        dates = [inquiry["date"]] + ([account["opened"]] if account.get("opened") else [])
        current = all((not rel.get("valid_from") or d >= rel["valid_from"]) and
                      (not rel.get("valid_to") or d <= rel["valid_to"]) for d in dates)
        # Missing validity dates or account dates cannot establish a historical affiliation.
        historical_complete = rel["relationship_type"] != "historical" or bool(rel.get("valid_from") and rel.get("valid_to") and account.get("opened"))
        trusted = rel.get("verification_status") == "verified" and bool(rel.get("evidence_source") and rel.get("verified_by") and rel.get("last_reviewed")) and current and historical_complete
        verified |= trusted
        reasons.append({"type": rel["relationship_type"], "relationship_id": rel["id"], "evidence": rel.get("evidence_source") or "No documented source", "verified_fact": trusted,
                        "required_verification": "Establish same transaction" if trusted else "Verify affiliation and applicable dates"})
    if iname != aname and SequenceMatcher(None, iname, aname).ratio() >= .72:
        reasons.append({"type": "name_similarity", "evidence": "Heuristic suggestion only; no verified affiliation", "verified_fact": False})
    if account.get("opened"):
        delta = (exact_date(account["opened"]) - exact_date(inquiry["date"])).days
        if -7 <= delta <= 90:
            reasons.append({"type": "date_proximity", "evidence": f"Account opened {delta} days after inquiry; timing is supporting evidence only", "verified_fact": False})
    elif not reasons:
        # Missing dates cannot rule out dealership, refinance, or limit-increase connections.
        reasons.append({"type": "missing_open_date", "evidence": "Account opening date unavailable; investigate possible application connection", "verified_fact": False})
    if not reasons:
        return None
    return {"account_id": account["id"], "account": account, "status": RELATED if verified else CHECK,
            "evidence": reasons, "required_verification": "Document whether this inquiry belongs to this account/application"}


def screen(report, relationships, reviews=None):
    validate_report(report)
    reviews = reviews or {}
    positive = [a for a in report["accounts"] if a["status"] == "open" or a["payment_history"] == "positive"]
    digest = fingerprint(report, relationships)
    rows = []
    for inquiry in report["inquiries"]:
        candidates = [p for a in positive if (p := pair(inquiry, a, relationships))]
        status = RELATED if any(p["status"] == RELATED for p in candidates) else CHECK if candidates else NONE
        review = reviews.get(inquiry["id"])
        if review and review.get("fingerprint") == digest:
            status = MATCH if review["outcome"] == "protected" else ELIGIBLE
        else:
            review = None
        duplicates = [i["id"] for i in report["inquiries"] if i["id"] != inquiry["id"] and
                      (normalized(i["name"]), i["bureau"], i["date"]) == (normalized(inquiry["name"]), inquiry["bureau"], inquiry["date"])]
        rows.append({"inquiry": inquiry, "status": status, "candidates": candidates,
                     "accounts_checked": len(positive), "screening_complete": True,
                     "responsible_agent": report.get("_submitted_by", "Unassigned — verify agent ownership"),
                     "confirmed_account": next((a for a in positive if review and review["outcome"] == "protected" and a["id"] == review.get("matched_account_id")), None),
                     "duplicate_ids": duplicates, "review": review,
                     "action": {MATCH: "Protect", RELATED: "Protection hold — QA verification", CHECK: "Protection hold — investigate", NONE: "Verify authorization", ELIGIBLE: "Letter approval review"}[status]})
    summary = {"total": len(rows), "protected": sum(r["status"] == MATCH for r in rows),
               "related_hold": sum(r["status"] == RELATED for r in rows),
               "manual_review": sum(r["status"] == CHECK for r in rows),
               "unmatched": sum(r["status"] == NONE for r in rows),
               "qa_candidates": sum(r["status"] == ELIGIBLE for r in rows),
               "duplicate_inquiries": sum(bool(r["duplicate_ids"]) for r in rows)}
    return {"fingerprint": digest, "summary": summary, "rows": rows, "source_report": report,
            "bureaus": {b: [r for r in rows if r["inquiry"]["bureau"] == b] for b in BUREAUS}}


def qa_review(report, relationships, reviews, inquiry_id, payload, reviewer):
    result = screen(report, relationships, reviews)
    row = next((r for r in result["rows"] if r["inquiry"]["id"] == inquiry_id), None)
    if not row:
        raise ValueError("Inquiry not found")
    if payload.get("fingerprint") != result["fingerprint"]:
        raise ValueError("Screening changed; reload before reviewing")
    if payload.get("outcome") not in ("protected", "eligible"):
        raise ValueError("Outcome must be protected or eligible")
    for key in ("evidence", "authorization_findings", "application_findings", "date_findings", "affiliation_findings", "auto_financing_findings"):
        required_text(payload, key)
    if not all(payload.get("checklist", {}).get(k) is True for k in CHECKLIST):
        raise ValueError("All eight pre-dispute checks are mandatory")
    findings = payload.get("account_findings", {})
    protected_accounts = [a for a in report["accounts"] if a["status"] == "open" or a["payment_history"] == "positive"]
    for account in protected_accounts:
        required_text(findings, account["id"])
    if payload["outcome"] == "protected":
        if payload.get("matched_account_id") not in [a["id"] for a in protected_accounts]:
            raise ValueError("Specify the confirmed positive account")
    else:
        required_text(payload, "factual_basis")
        if payload.get("authorization_result") != "documented_basis_to_challenge":
            raise ValueError("No match alone is insufficient: document an authorization/permissible-purpose challenge")
        if row["status"] == MATCH:
            raise ValueError("Confirmed protected inquiries cannot be dispute candidates")
    return {**payload, "reviewer": reviewer, "fingerprint": result["fingerprint"], "reviewed_at": date.today().isoformat()}


def parse_letter(text):
    """Strict CSV avoids guessing at dates or arbitrary document extraction."""
    reader = csv.DictReader(io.StringIO(text.removeprefix("\ufeff")))
    if reader.fieldnames != ["bureau", "name", "date"]:
        raise ValueError("Inquiry schedule must have CSV header bureau,name,date; upload documents through the document importer and verify extracted entries")
    entries = []
    for line, item in enumerate(reader, 2):
        if None in item or any(value is None for value in item.values()):
            raise ValueError(f"Malformed letter row {line}")
        required_text(item, "name")
        exact_date(item["date"])
        if item["bureau"] not in BUREAUS:
            raise ValueError(f"Invalid bureau at row {line}")
        entries.append(item)
    if not entries:
        raise ValueError("Letter inquiry list is empty")
    if len(entries) > 1000:
        raise ValueError("Maximum 1000 letter entries")
    return entries


def cross_check(text, screening):
    entries = parse_letter(text)
    seen = set()
    checks = []
    for entry in entries:
        key = (entry["bureau"], entry["name"], entry["date"])
        exact = [r for r in screening["rows"] if (r["inquiry"]["bureau"], r["inquiry"]["name"], r["inquiry"]["date"]) == key]
        issues = []
        if key in seen:
            issues.append("Duplicate letter entry requiring verification")
        seen.add(key)
        if not exact:
            issues.append("Inquiry not present with this exact creditor name, date, and bureau; verify transcription against source")
            near = [r for r in screening["rows"] if normalized(r["inquiry"]["name"]) == normalized(entry["name"])]
            if near:
                issues.append("Possible incorrect creditor spelling, inquiry date, or bureau assignment")
        elif len(exact) > 1 or exact[0]["duplicate_ids"]:
            issues.append("Duplicate source inquiries require source correction or verification; no records merged")
        for row in exact:
            if row["status"] != ELIGIBLE:
                issues.append("CRITICAL WARNING — POSSIBLE POSITIVE ACCOUNT CONNECTION" if row["status"] in (MATCH, RELATED, CHECK) else "Authorization review and documented QA approval required")
        checks.append({"entry": entry, "issues": issues, "matching_results": exact})
    approval_fingerprint = hashlib.sha256(json.dumps({"source": screening["fingerprint"],
        "reviews": [r["review"] for r in screening["rows"]]}, sort_keys=True).encode()).hexdigest()
    return {"state": "BLOCKED" if any(c["issues"] for c in checks) else "QA LETTER APPROVAL REQUIRED", "checks": checks,
            "letter_hash": hashlib.sha256(text.encode()).hexdigest(), "fingerprint": approval_fingerprint}

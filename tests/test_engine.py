import copy
import unittest

from credit_checker.engine import (CHECK, CHECKLIST, ELIGIBLE, MATCH, NONE, RELATED,
    cross_check, fingerprint, parse_letter, qa_review, screen, validate_relationship, validate_report)


def report():
    return {"inquiries": [{"id": "i", "name": "JPMCB", "date": "2026-05-10", "bureau": "TU"}],
            "accounts": [{"id": "a", "name": "JPMORGAN CHASE BANK", "opened": "2026-05-12", "bureau": "EQ",
                          "status": "open", "payment_history": "positive", "product": "credit_card"}]}


def relationship():
    return {"id": "r", "inquiry_company": "JPMCB", "official_creditor": "JPMORGAN CHASE BANK",
            "relationship_type": "alias", "evidence_source": "Test fixture source — not a published creditor fact",
            "verification_status": "verified", "last_reviewed": "2026-06-01", "verified_by": "admin"}


def review(r, rels, outcome="eligible"):
    return {"fingerprint": fingerprint(r, rels), "outcome": outcome, "matched_account_id": "a",
            "evidence": "Test supporting documentation", "authorization_findings": "Reviewed application records",
            "application_findings": "Documented transaction findings", "date_findings": "Dates reviewed",
            "affiliation_findings": "Issuer evidence reviewed", "auto_financing_findings": "Reviewed; not applicable",
            "account_findings": {a["id"]: "Test evidence rules connection out" for a in r["accounts"]},
            "authorization_result": "documented_basis_to_challenge", "factual_basis": "Documented specific inaccurate inquiry",
            "checklist": {k: True for k in CHECKLIST}}


class EngineTests(unittest.TestCase):
    def test_all_bureaus_and_positive_closed_accounts(self):
        r = report()
        r["accounts"].append({**r["accounts"][0], "id": "b", "status": "closed", "bureau": "EX"})
        r["accounts"].append({**r["accounts"][0], "id": "c", "status": "closed", "payment_history": "negative"})
        result = screen(r, [])
        self.assertEqual(result["rows"][0]["accounts_checked"], 2)
        self.assertEqual(len(result["rows"][0]["candidates"]), 2)
        self.assertEqual(set(result["bureaus"]), {"TU", "EX", "EQ"})

    def test_same_name_not_transaction_proof(self):
        r = report(); r["inquiries"][0]["name"] = r["accounts"][0]["name"]
        self.assertEqual(screen(r, [])["rows"][0]["status"], RELATED)

    def test_date_proximity_only_hold(self):
        self.assertEqual(screen(report(), [])["rows"][0]["status"], CHECK)

    def test_verified_alias_affiliation_only(self):
        self.assertEqual(screen(report(), [relationship()])["rows"][0]["status"], RELATED)

    def test_unverified_or_expired_relationship(self):
        for changes in ({"verification_status": "unverified"}, {"valid_to": "2025-12-31"}, {"evidence_source": ""}, {"relationship_type": "historical"}):
            self.assertEqual(screen(report(), [{**relationship(), **changes}])["rows"][0]["status"], CHECK)

    def test_historical_relationship_applicable_to_both_dates(self):
        rel = {**relationship(), "relationship_type": "historical", "valid_from": "2026-01-01", "valid_to": "2026-05-11"}
        self.assertEqual(screen(report(), [rel])["rows"][0]["status"], CHECK)
        rel["valid_to"] = "2026-06-01"
        self.assertEqual(screen(report(), [rel])["rows"][0]["status"], RELATED)

    def test_missing_dates_hold(self):
        r = report(); del r["accounts"][0]["opened"]
        self.assertEqual(screen(r, [])["rows"][0]["status"], CHECK)

    def test_no_match_not_automatically_eligible(self):
        r = report(); r["accounts"] = []
        self.assertEqual(screen(r, [])["rows"][0]["status"], NONE)
        self.assertEqual(cross_check("bureau,name,date\nTU,JPMCB,2026-05-10", screen(r, []))["state"], "BLOCKED")

    def test_qa_protected_and_no_override(self):
        r = report(); rels = [relationship()]
        saved = qa_review(r, rels, {}, "i", review(r, rels, "protected"), "qa")
        reviews = {"i": saved}
        self.assertEqual(screen(r, rels, reviews)["rows"][0]["status"], MATCH)
        with self.assertRaises(ValueError):
            qa_review(r, rels, reviews, "i", review(r, rels), "qa")
        result = cross_check("bureau,name,date\nTU,JPMCB,2026-05-10", screen(r, rels, reviews))
        self.assertEqual(result["state"], "BLOCKED")
        self.assertIn("CRITICAL WARNING", result["checks"][0]["issues"][0])

    def test_qa_requires_all_checks_and_each_account_and_basis(self):
        r = report()
        for key, value in (("checklist", {}), ("account_findings", {}), ("factual_basis", ""), ("authorization_result", "unknown"), ("evidence", "")):
            payload = review(r, []); payload[key] = value
            with self.assertRaises(ValueError):
                qa_review(r, [], {}, "i", payload, "qa")

    def test_review_stale_after_report_or_registry_change(self):
        r = report(); payload = review(r, [])
        saved = qa_review(r, [], {}, "i", payload, "qa")
        self.assertEqual(screen(r, [], {"i": saved})["rows"][0]["status"], ELIGIBLE)
        self.assertNotEqual(screen(r, [relationship()], {"i": saved})["rows"][0]["status"], ELIGIBLE)
        changed = copy.deepcopy(r); changed["inquiries"][0]["date"] = "2026-05-11"
        self.assertNotEqual(screen(changed, [], {"i": saved})["rows"][0]["status"], ELIGIBLE)
        with self.assertRaises(ValueError):
            qa_review(changed, [], {}, "i", payload, "qa")

    def test_eligible_letter_needs_separate_final_approval(self):
        r = report(); saved = qa_review(r, [], {}, "i", review(r, []), "qa")
        result = cross_check("bureau,name,date\nTU,JPMCB,2026-05-10", screen(r, [], {"i": saved}))
        self.assertEqual(result["state"], "QA LETTER APPROVAL REQUIRED")

    def test_letter_mistakes_and_duplicates_block(self):
        r = report(); saved = qa_review(r, [], {}, "i", review(r, []), "qa")
        result = screen(r, [], {"i": saved})
        for text in ("EX,JPMCB,2026-05-10", "TU,JPMCB,2026-05-11", "TU,jpmcb,2026-05-10", "TU,UNKNOWN,2026-05-10", "TU,JPMCB,2026-05-10\nTU,JPMCB,2026-05-10"):
            self.assertEqual(cross_check("bureau,name,date\n" + text, result)["state"], "BLOCKED")

    def test_source_duplicates_preserved(self):
        r = report(); r["inquiries"].append({**r["inquiries"][0], "id": "i2"})
        result = screen(r, [])
        self.assertEqual(result["summary"]["total"], 2)
        self.assertEqual(result["summary"]["duplicate_inquiries"], 2)
        self.assertEqual(result["rows"][0]["duplicate_ids"], ["i2"])

    def test_input_validation_and_no_freeform_guessing(self):
        for text in ("Dispute JPMCB on May 10", "bureau,name,date\nTU,X,05/10/2026", "bureau,name,date\n", "bureau,name,date\nTU,X,2026-05-10,extra"):
            with self.assertRaises(ValueError): parse_letter(text)
        r = report(); r["inquiries"][0]["date"] = "2026-02-30"
        with self.assertRaises(ValueError): validate_report(r)
        with self.assertRaises(ValueError): validate_relationship({**relationship(), "evidence_source": ""}, True)


if __name__ == "__main__":
    unittest.main()

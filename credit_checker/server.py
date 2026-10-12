"""Local development API. Identity and approvals come from server-side bindings."""
import hmac
import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import unquote
from contextlib import nullcontext

from .engine import cross_check, fingerprint, qa_review, screen, validate_relationship, validate_report
from .documents import extract_upload

ROOT = Path(__file__).resolve().parent.parent


class Store:
    def __init__(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(path)
        os.chmod(path, 0o600)
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS records(kind TEXT, id TEXT, body TEXT, PRIMARY KEY(kind,id));
        CREATE TABLE IF NOT EXISTS audit(sequence INTEGER PRIMARY KEY, at TEXT, actor TEXT, action TEXT, detail TEXT);
        """)

    def get(self, kind, ident):
        row = self.db.execute("SELECT body FROM records WHERE kind=? AND id=?", (kind, ident)).fetchone()
        return json.loads(row[0]) if row else None

    def all(self, kind):
        return [json.loads(r[0]) for r in self.db.execute("SELECT body FROM records WHERE kind=? ORDER BY id", (kind,))]

    def put(self, kind, ident, body, actor, action):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO records VALUES(?,?,?)", (kind, ident, json.dumps(body)))
            self.db.execute("INSERT INTO audit(at,actor,action,detail) VALUES(?,?,?,?)",
                            (datetime.now(timezone.utc).isoformat(), actor, action, json.dumps({"kind": kind, "id": ident, "body": body})))

    def request_lock(self):
        return nullcontext()

    def audit_events(self):
        return [dict(zip(("sequence", "at", "actor", "action", "detail"), row))
                for row in self.db.execute("SELECT * FROM audit ORDER BY sequence DESC")]

    def activity(self, ident):
        events = []
        for event in self.audit_events():
            metadata = json.loads(event["detail"])
            if metadata["id"] == ident or metadata["body"].get("report_id") == ident:
                events.append({k: event[k] for k in ("sequence", "at", "actor", "action")})
            if len(events) == 30:
                break
        return events


def make_handler(store, identities):
    configured = [entry["token"] for entry in identities.values()]
    if any(len(token) < 16 for token in configured) or len(set(configured)) != len(configured):
        raise ValueError("Use distinct identity tokens of at least 16 characters")

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(120)

        def log_message(self, *args):
            pass  # Avoid logging client financial data or credentials.

        def send_json(self, status, data):
            self.responded = True
            encoded = json.dumps(data).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(encoded)

        def identity(self, roles):
            # Keep app identity separate from Cloud Run's Google IAM bearer token.
            token = self.headers.get("X-CQC-Token") or self.headers.get("Authorization", "").removeprefix("Bearer ")
            for name, entry in identities.items():
                if hmac.compare_digest(token, entry["token"]) and entry["role"] in roles:
                    return name
            raise PermissionError("A configured identity with the required role is needed")

        def state(self, ident):
            ident = unquote(ident)
            report = store.get("report", ident)
            if report is None:
                raise ValueError("Report not found")
            relationships = store.all("relationship")
            reviews = store.get("reviews", ident) or {}
            return report, relationships, reviews

        def checked_letter(self, payload, report, relationships, reviews):
            data = cross_check(payload["text"], screen(report, relationships, reviews))
            document_id = payload.get("document_id")
            if document_id:
                document = store.get("document", document_id)
                if document is None or document["kind"] != "letter":
                    raise ValueError("Uploaded letter document not found")
                data.update(document_id=document_id, filename=document["filename"], source_hash=document["source_hash"],
                            document_issues=document["notes"], requires_document_verification=True)
                data["letter_hash"] = hashlib.sha256(json.dumps([payload["text"], document["source_hash"]]).encode()).hexdigest()
                if document["unreadable_pages"]:
                    data["state"] = "BLOCKED"
                    data["blocking_document_issue"] = "Document contains unreadable pages. Upload a fully readable/OCR document before approval."
            return data

        def do_GET(self):
            self._guard(self._get)

        def do_POST(self):
            self._guard(self._post)

        def _guard(self, operation):
            self.responded = False
            try:
                # Static UI and health do not need cloud state or a workflow lease.
                if not self.path.startswith("/api/"):
                    operation()
                    return
                self.identity(("agent", "qa", "admin"))
                with store.request_lock():
                    operation()
            except PermissionError as error:
                self.send_json(403, {"error": str(error)})
            except Exception:
                # Storage failures must never bypass QA gates or expose credentials.
                if not self.responded:
                    self.send_json(503, {"error": "Storage is unavailable or another review is in progress; retry shortly"})

        def _get(self):
            try:
                if self.path in ("/", "/app.js", "/style.css"):
                    name = {"/": "index.html", "/app.js": "app.js", "/style.css": "style.css"}[self.path]
                    content = (ROOT / "static" / name).read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", {"index.html": "text/html", "app.js": "text/javascript", "style.css": "text/css"}[name])
                    self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'; frame-ancestors 'none'")
                    self.end_headers()
                    self.wfile.write(content)
                    return
                if self.path == "/health":
                    self.send_json(200, {"status": "ok"})
                    return
                actor = self.identity(("agent", "qa", "admin"))
                if self.path == "/api/session":
                    data = {"actor": actor, "role": identities[actor]["role"]}
                elif self.path == "/api/relationships":
                    data = store.all("relationship")
                elif self.path.startswith("/api/report/"):
                    ident = self.path[len("/api/report/"):]
                    report, relationships, reviews = self.state(ident)
                    data = screen(report, relationships, reviews)
                elif self.path.startswith("/api/letter/"):
                    saved = store.get("letter", self.path[len("/api/letter/"):])
                    if saved is None:
                        raise ValueError("Approved letter not found")
                    report, relationships, reviews = self.state(saved["report_id"])
                    current = self.checked_letter(saved, report, relationships, reviews)
                    data = saved if current["fingerprint"] == saved["fingerprint"] and current["state"] != "BLOCKED" else {
                        **current, "state": "BLOCKED", "reason": "Earlier approval invalidated by report or evidence changes; fresh QA approval required"}
                elif self.path.startswith("/api/document/"):
                    data = store.get("document", unquote(self.path[len("/api/document/"):]))
                    if data is None:
                        raise ValueError("Document not found")
                elif self.path.startswith("/api/activity/"):
                    ident = unquote(self.path[len("/api/activity/"):])
                    data = store.activity(ident)
                elif self.path == "/api/audit":
                    self.identity(("admin",))
                    data = store.audit_events()
                else:
                    self.send_json(404, {"error": "Not found"})
                    return
                self.send_json(200, data)
            except PermissionError as error:
                self.send_json(403, {"error": str(error)})
            except (ValueError, TypeError, KeyError) as error:
                self.send_json(400, {"error": str(error)})

        def _post(self):
            try:
                actor = self.identity(("agent", "qa", "admin"))
                size = int(self.headers.get("Content-Length", "0"))
                limit = 15_000_000 if self.path == "/api/document" else 2_000_000
                if not 0 < size <= limit:
                    raise ValueError(f"Request body must be between 1 and {limit:,} bytes")
                payload = json.loads(self.rfile.read(size))
                if not isinstance(payload, dict):
                    raise ValueError("Request must be a JSON object")
                if self.path == "/api/document":
                    document = extract_upload(payload, actor)
                    store.put("document", document["id"], document, actor, "document_uploaded")
                    data = {k: v for k, v in document.items() if k != "content_base64"}
                elif self.path == "/api/report":
                    ident = payload["id"]
                    if not isinstance(ident, str) or not ident.strip() or "/" in ident:
                        raise ValueError("Report ID must be nonempty text without slashes")
                    report = validate_report(payload["report"])
                    report["_submitted_by"] = actor
                    # Client-supplied internal provenance is never trusted.
                    for key in ("_document_id", "_source_hash", "_source_verified"):
                        report.pop(key, None)
                    if payload.get("document_id"):
                        document = store.get("document", payload["document_id"])
                        if not document or document["kind"] != "source":
                            raise ValueError("Uploaded source document not found")
                        if document["unreadable_pages"]:
                            raise ValueError("Source contains unreadable pages; upload a readable/OCR document")
                        if payload.get("source_verified") is not True:
                            raise ValueError("Verify all extracted inquiries and all open/positive accounts against the original source")
                        report.update(_document_id=document["id"], _source_hash=document["source_hash"], _source_verified=True)
                    store.put("report", ident, report, actor, "report_saved")
                    data = screen(report, store.all("relationship"), store.get("reviews", ident))
                elif self.path == "/api/relationship":
                    self.identity(("admin",))
                    record = payload["record"]
                    verify = payload.get("verify") is True
                    record["verification_status"] = "verified" if verify else "unverified"
                    record["verified_by"] = actor if verify else None
                    if verify:
                        record["last_reviewed"] = datetime.now(timezone.utc).date().isoformat()
                    validate_relationship(record, verify)
                    store.put("relationship", record["id"], record, actor, "relationship_verified" if verify else "relationship_saved")
                    data = record
                elif self.path == "/api/review":
                    self.identity(("qa",))
                    ident = payload["report_id"]
                    report, relationships, reviews = self.state(ident)
                    review = qa_review(report, relationships, reviews, payload["inquiry_id"], payload["review"], actor)
                    reviews[payload["inquiry_id"]] = review
                    store.put("reviews", ident, reviews, actor, "inquiry_reviewed")
                    data = screen(report, relationships, reviews)
                elif self.path in ("/api/letter/check", "/api/letter/approve"):
                    report, relationships, reviews = self.state(payload["report_id"])
                    data = self.checked_letter(payload, report, relationships, reviews)
                    if self.path.endswith("approve"):
                        self.identity(("qa",))
                        if data["state"] == "BLOCKED":
                            raise ValueError("Letter approval blocked; submit an updated final letter resolving all issues")
                        if payload.get("fingerprint") != data["fingerprint"] or payload.get("letter_hash") != data["letter_hash"]:
                            raise ValueError("Report, evidence, or letter changed; cross-check again")
                        if not isinstance(payload.get("approval_evidence"), str) or not payload["approval_evidence"].strip():
                            raise ValueError("Document final letter approval evidence")
                        if data.get("requires_document_verification") and payload.get("document_verified") is not True:
                            raise ValueError("QA must verify the full uploaded letter and confirm the inquiry schedule is complete and accurate")
                        data.update(state="READY TO SEND", approved_by=actor, approval_evidence=payload["approval_evidence"],
                                    report_id=payload["report_id"], text=payload["text"], document_verified=payload.get("document_verified") is True)
                        store.put("letter", data["letter_hash"], data, actor, "letter_approved")
                else:
                    self.send_json(404, {"error": "Not found"})
                    return
                self.send_json(200, data)
            except PermissionError as error:
                self.send_json(403, {"error": str(error)})
            except (ValueError, TypeError, KeyError, AttributeError) as error:
                self.send_json(400, {"error": str(error)})
    return Handler


def main():
    # Configure distinct individual identities; no client-supplied role or reviewer is trusted.
    identities = json.loads(os.environ.get("CQC_IDENTITIES", "{}"))
    for name, entry in identities.items():
        if not name or entry.get("role") not in ("agent", "qa", "admin"):
            raise ValueError("Each identity needs a name, role (agent/qa/admin), and token")
    storage_backend = os.environ.get("CQC_STORAGE", "sqlite")
    if storage_backend == "google":
        allowed = {"CQC_PROJECT": "quality-checker-511418", "CQC_BUCKET": "quality-checker-511418-documents",
                   "CQC_DATABASE": "quality-checker-data", "CQC_NAMESPACE": "quality-checker"}
        if any(os.environ.get(key) != value for key, value in allowed.items()):
            raise ValueError("Cloud storage must target the isolated Quality Checker resources exactly")
        from .cloud_store import CloudStore
        store = CloudStore(os.environ.get("CQC_PROJECT"), os.environ.get("CQC_BUCKET"),
                           namespace=os.environ.get("CQC_NAMESPACE", "credit-quality-checker"),
                           database=os.environ.get("CQC_DATABASE", "(default)"))
    elif storage_backend == "sqlite":
        if os.environ.get("K_SERVICE") and os.environ.get("CQC_EPHEMERAL_PREVIEW") != "1":
            raise ValueError("Cloud Run requires CQC_STORAGE=google; ephemeral SQLite is permitted only for an explicit CQC_EPHEMERAL_PREVIEW=1 preview")
        if os.environ.get("K_SERVICE") and identities:
            raise ValueError("Ephemeral Cloud Run previews cannot accept authenticated financial-data operations")
        store = Store(os.environ.get("CQC_DB", str(ROOT / ".local" / "checker.sqlite3")))
    else:
        raise ValueError("CQC_STORAGE must be sqlite or google")
    host = os.environ.get("HOST", "0.0.0.0" if os.environ.get("K_SERVICE") else "127.0.0.1")
    server = HTTPServer((host, int(os.environ.get("PORT", "8000"))), make_handler(store, identities))
    server.timeout = 120
    print(f"Credit checker listening on {host}:{server.server_port}", flush=True)
    if not identities:
        print("No identities configured: UI and health only; API access is disabled.", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()

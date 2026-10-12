"""Firestore pointers and immutable Cloud Storage bodies for durable Cloud Run state.

Bodies live in Cloud Storage so document uploads never exceed Firestore's 1 MiB
document limit. A transaction atomically records the current pointer and audit.
"""
import hashlib
import json
import secrets
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from google.api_core.exceptions import PreconditionFailed
from google.cloud import firestore, storage


class StorageBusy(RuntimeError):
    pass


class CloudStore:
    def __init__(self, project, bucket_name, namespace="credit-quality-checker", database="(default)"):
        if not project or not bucket_name:
            raise ValueError("CQC_PROJECT and CQC_BUCKET are required for Google Cloud storage")
        self.firestore = firestore.Client(project=project, database=database)
        self.bucket = storage.Client(project=project).bucket(bucket_name)
        self.root = self.firestore.collection("credit_checker").document(namespace)
        self.records = self.root.collection("records")
        self.audits = self.root.collection("audit")
        self.lock_ref = self.root.collection("locks").document("workflow")
        self.prefix = f"{namespace}/records/"
        self.lock_token = None

    def ref(self, kind, ident):
        key = hashlib.sha256(json.dumps([kind, ident]).encode()).hexdigest()
        return self.records.document(key)

    def body(self, pointer):
        raw = self.bucket.blob(pointer["object"]).download_as_bytes(checksum="auto")
        if hashlib.sha256(raw).hexdigest() != pointer["sha256"]:
            raise RuntimeError("Stored record integrity check failed")
        return json.loads(raw)

    def get(self, kind, ident):
        snapshot = self.ref(kind, ident).get()
        return self.body(snapshot.to_dict()) if snapshot.exists else None

    def all(self, kind):
        records = [s.to_dict() for s in self.records.where(filter=firestore.FieldFilter("kind", "==", kind)).stream()]
        return [self.body(record) for record in sorted(records, key=lambda r: r["id"])]

    @contextmanager
    def request_lock(self):
        """Serialize state/QA checks across instances, including overlapping revisions."""
        token = secrets.token_hex(24)
        transaction = self.firestore.transaction()

        @firestore.transactional
        def acquire(tx):
            snapshot = self.lock_ref.get(transaction=tx)
            now = datetime.now(timezone.utc)
            if snapshot.exists and snapshot.to_dict()["expires"] > now:
                raise StorageBusy("Another review is in progress; retry shortly")
            tx.set(self.lock_ref, {"token": token, "expires": now + timedelta(seconds=180)})

        acquire(transaction)
        self.lock_token = token
        try:
            yield
        finally:
            @firestore.transactional
            def release(tx):
                snapshot = self.lock_ref.get(transaction=tx)
                if snapshot.exists and snapshot.to_dict()["token"] == token:
                    tx.delete(self.lock_ref)
            try:
                release(self.firestore.transaction())
            finally:
                self.lock_token = None

    def put(self, kind, ident, body, actor, action):
        raw = json.dumps(body, sort_keys=True).encode()
        digest = hashlib.sha256(raw).hexdigest()
        object_name = self.prefix + digest + ".json"
        blob = self.bucket.blob(object_name)
        if not blob.exists():
            try:
                blob.upload_from_string(raw, content_type="application/json", if_generation_match=0, checksum="auto")
            except PreconditionFailed:
                # Another writer created the immutable object; verify before reuse.
                if hashlib.sha256(blob.download_as_bytes(checksum="auto")).hexdigest() != digest:
                    raise RuntimeError("Stored record integrity check failed")
        elif hashlib.sha256(blob.download_as_bytes(checksum="auto")).hexdigest() != digest:
            raise RuntimeError("Stored record integrity check failed")
        pointer = {"kind": kind, "id": ident, "object": object_name, "sha256": digest}

        @firestore.transactional
        def commit(tx):
            snapshot = self.lock_ref.get(transaction=tx)
            if not self.lock_token or not snapshot.exists:
                raise StorageBusy("Review lease is unavailable; retry")
            lock = snapshot.to_dict()
            if lock["token"] != self.lock_token or lock["expires"] <= datetime.now(timezone.utc):
                raise StorageBusy("Review lease expired; reload and retry")
            tx.set(self.ref(kind, ident), pointer)
            event = {**pointer, "at": datetime.now(timezone.utc), "actor": actor, "action": action}
            audit_ref = self.audits.document()
            tx.set(audit_ref, event)
            report_id = ident if kind in ("report", "reviews") else body.get("report_id")
            if report_id:
                key = hashlib.sha256(report_id.encode()).hexdigest()
                tx.set(self.root.collection("checks").document(key).collection("activity").document(audit_ref.id),
                       {k: event[k] for k in ("at", "actor", "action")})

        commit(self.firestore.transaction())

    def audit_events(self):
        events = []
        for snapshot in self.audits.order_by("at", direction=firestore.Query.DESCENDING).limit(100).stream():
            event = snapshot.to_dict()
            # Original binary uploads are available through the document endpoint,
            # not duplicated in audit responses.
            body = self.body(event) if event["kind"] != "document" else {"object": event["object"], "sha256": event["sha256"]}
            events.append({"sequence": snapshot.id, "at": event["at"].isoformat(), "actor": event["actor"],
                           "action": event["action"], "detail": json.dumps({"kind": event["kind"], "id": event["id"], "body": body})})
        return events

    def activity(self, ident):
        key = hashlib.sha256(ident.encode()).hexdigest()
        query = self.root.collection("checks").document(key).collection("activity").order_by("at", direction=firestore.Query.DESCENDING).limit(30)
        return [{"sequence": s.id, **s.to_dict(), "at": s.to_dict()["at"].isoformat()} for s in query.stream()]

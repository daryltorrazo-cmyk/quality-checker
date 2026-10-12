"""Adapter contracts tested with in-memory SDK fakes; no Google resources used."""
import copy
import json
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

try:
    from credit_checker.cloud_store import CloudStore, StorageBusy, PreconditionFailed
except ModuleNotFoundError:
    CloudStore = None


class Snapshot:
    def __init__(self, ref):
        self.id = ref.id
        self.value = copy.deepcopy(ref.db.data.get(ref.path))
        self.exists = self.value is not None

    def to_dict(self):
        return copy.deepcopy(self.value)


class Ref:
    def __init__(self, db, path):
        self.db, self.path = db, path
        self.id = path.split('/')[-1]

    def collection(self, name):
        return Collection(self.db, self.path + '/' + name)

    def get(self, transaction=None):
        return Snapshot(self)


class Collection:
    def __init__(self, db, path):
        self.db, self.path = db, path
        self.filter = None
        self.order = None
        self.count = None

    def document(self, ident=None):
        return Ref(self.db, self.path + '/' + (ident or uuid.uuid4().hex))

    def where(self, filter):
        self.filter = filter
        return self

    def order_by(self, field, direction):
        self.order = field
        return self

    def limit(self, count):
        self.count = count
        return self

    def stream(self):
        rows = [(key, value) for key, value in self.db.data.items() if key.startswith(self.path + '/') and key.count('/') == self.path.count('/') + 1]
        if self.filter:
            rows = [(key, value) for key, value in rows if value.get(self.filter.field_path) == self.filter.value]
        if self.order:
            rows.sort(key=lambda row: row[1][self.order], reverse=True)
        return [Snapshot(Ref(self.db, key)) for key, _ in rows[:self.count]]


class Database:
    def __init__(self):
        self.data = {}

    def collection(self, name):
        return Collection(self, name)

    def transaction(self):
        return Transaction(self)


class Transaction:
    def __init__(self, db):
        self.db, self.writes = db, []

    def set(self, ref, value):
        self.writes.append((ref.path, copy.deepcopy(value)))

    def delete(self, ref):
        self.writes.append((ref.path, None))


def transactional(fn):
    def run(tx):
        result = fn(tx)
        for path, value in tx.writes:
            if value is None:
                tx.db.data.pop(path, None)
            else:
                tx.db.data[path] = value
        return result
    return run


class Bucket:
    def __init__(self):
        self.objects = {}

    def blob(self, key):
        bucket = self
        class Blob:
            def exists(self):
                return key in bucket.objects

            def upload_from_string(self, raw, **kwargs):
                if key in bucket.objects:
                    raise PreconditionFailed('Immutable object already exists')
                bucket.objects[key] = raw

            def download_as_bytes(self, **kwargs):
                return bucket.objects[key]
        return Blob()


@unittest.skipIf(CloudStore is None, 'Install requirements-cloud.txt to test the Google storage adapter')
class CloudStoreTests(unittest.TestCase):
    def setUp(self):
        self.db, self.bucket = Database(), Bucket()
        self.db_patch = patch('credit_checker.cloud_store.firestore.Client', return_value=self.db)
        self.storage_patch = patch('credit_checker.cloud_store.storage.Client')
        self.tx_patch = patch('credit_checker.cloud_store.firestore.transactional', transactional)
        self.db_client = self.db_patch.start()
        self.storage_patch.start().return_value.bucket.return_value = self.bucket
        self.tx_patch.start()
        self.addCleanup(self.db_patch.stop)
        self.addCleanup(self.storage_patch.stop)
        self.addCleanup(self.tx_patch.stop)
        self.store = CloudStore('test-project', 'test-bucket', database='isolated-db')

    def test_large_upload_retained_outside_firestore_and_audited(self):
        body = {'text':'x' * 2_000_000, 'filename':'test-letter.txt'}
        with self.store.request_lock():
            self.store.put('document', 'doc', body, 'agent', 'document_uploaded')
        self.assertEqual(self.store.get('document', 'doc'), body)
        self.assertEqual(self.store.all('document'), [body])
        self.assertTrue(all(len(json.dumps(value, default=str)) < 1000 for value in self.db.data.values()))
        self.assertEqual(len(self.store.audit_events()), 1)
        self.db_client.assert_called_with(project='test-project', database='isolated-db')

    def test_overlapping_revisions_cannot_review_simultaneously(self):
        other = CloudStore('test-project', 'test-bucket', database='isolated-db')
        with self.store.request_lock():
            with self.assertRaises(StorageBusy):
                with other.request_lock():
                    pass
        with other.request_lock():
            other.put('report', 'check', {'inquiries':[], 'accounts':[]}, 'agent', 'report_saved')
        self.assertIsNotNone(self.store.get('report', 'check'))

    def test_expired_or_absent_lease_never_commits(self):
        with self.assertRaises(StorageBusy):
            self.store.put('letter', 'unapproved', {'state':'READY TO SEND'}, 'qa', 'letter_approved')
        self.assertIsNone(self.store.get('letter', 'unapproved'))
        with self.store.request_lock():
            self.db.data[self.store.lock_ref.path]['expires'] = datetime.now(timezone.utc) - timedelta(seconds=1)
            with self.assertRaises(StorageBusy):
                self.store.put('letter', 'expired', {'state':'READY TO SEND'}, 'qa', 'letter_approved')
        self.assertIsNone(self.store.get('letter', 'expired'))
        self.assertEqual(self.store.audit_events(), [])

    def test_content_integrity_and_immutable_history(self):
        with self.store.request_lock():
            self.store.put('report', 'check', {'value':'old'}, 'agent', 'report_saved')
            self.store.put('report', 'check', {'value':'new'}, 'agent', 'report_saved')
            self.store.put('report', 'check', {'value':'new'}, 'agent', 'report_saved')
        self.assertEqual(self.store.get('report', 'check'), {'value':'new'})
        self.assertEqual(len(self.bucket.objects), 2)
        self.assertEqual(len(self.store.activity('check')), 3)
        pointer = self.store.ref('report','check').get().to_dict()
        self.bucket.objects[pointer['object']] = b'{"value":"tampered"}'
        with self.assertRaises(RuntimeError):
            self.store.get('report','check')

    def test_new_namespace_does_not_modify_existing_records(self):
        self.db.data['existing-app/reports/client'] = {'untouched':True}
        with self.store.request_lock():
            self.store.put('report','check', {'value':'new'}, 'agent','report_saved')
        self.assertEqual(self.db.data['existing-app/reports/client'], {'untouched':True})

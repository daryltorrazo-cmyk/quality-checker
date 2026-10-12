import json
import base64
import multiprocessing
import tempfile
import unittest
import urllib.error
import urllib.request
from http.server import HTTPServer
from pathlib import Path

from credit_checker.server import Store, make_handler
from test_engine import report, review, relationship

IDENTITIES = {role: {"role": role, "token": role + "-test-only-token-123456"} for role in ("agent", "qa", "admin")}


def serve(path, pipe):
    server = HTTPServer(("127.0.0.1", 0), make_handler(Store(path), IDENTITIES))
    pipe.send(server.server_port)
    server.serve_forever()


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        parent, child = multiprocessing.Pipe()
        cls.process = multiprocessing.Process(target=serve, args=(str(Path(cls.temp.name) / "test.sqlite"), child))
        cls.process.start()
        if not parent.poll(10): raise RuntimeError("Server failed to start")
        cls.url = f"http://127.0.0.1:{parent.recv()}"

    @classmethod
    def tearDownClass(cls):
        cls.process.terminate(); cls.process.join(); cls.temp.cleanup()

    def request(self, path, body=None, role="agent"):
        headers = {"Content-Type": "application/json"}
        if role: headers["Authorization"] = "Bearer " + IDENTITIES[role]["token"]
        req = urllib.request.Request(self.url + path, data=json.dumps(body).encode() if body is not None else None, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def test_access_and_roles(self):
        self.assertEqual(self.request("/health", role=None)[0], 200)
        self.assertEqual(self.request("/api/relationships", role=None)[0], 403)
        self.assertEqual(self.request("/api/relationship", {"record": relationship(), "verify": True})[0], 403)
        self.assertEqual(self.request("/api/review", {})[0], 403)
        req = urllib.request.Request(self.url + '/api/session', headers={
            'Authorization':'Bearer Google-IAM-test-token', 'X-CQC-Token':IDENTITIES['qa']['token']})
        with urllib.request.urlopen(req, timeout=5) as response:
            self.assertEqual(json.loads(response.read())['role'], 'qa')

    def test_full_review_and_final_letter_flow(self):
        r = report(); ident = "integration"
        status, result = self.request("/api/report", {"id": ident, "report": r})
        self.assertEqual(status, 200)
        payload = review(r, []); payload["fingerprint"] = result["fingerprint"]
        letter = {"report_id": ident, "text": "bureau,name,date\nTU,JPMCB,2026-05-10"}
        self.assertEqual(self.request("/api/letter/check", letter)[1]["state"], "BLOCKED")
        self.assertEqual(self.request("/api/letter/approve", letter, "qa")[0], 400)
        self.assertEqual(self.request("/api/review", {"report_id": ident, "inquiry_id": "i", "review": payload}, "qa")[0], 200)
        _, checked = self.request("/api/letter/check", letter)
        approval = {**letter, "letter_hash": checked["letter_hash"], "fingerprint": checked["fingerprint"], "approval_evidence": "Final schedule reviewed against source"}
        self.assertEqual(self.request("/api/letter/approve", approval, "agent")[0], 403)
        self.assertEqual(self.request("/api/letter/approve", approval, "qa")[1]["state"], "READY TO SEND")
        self.assertEqual(self.request("/api/letter/approve", {**approval, "text": letter["text"] + "\nTU,JPMCB,2026-05-10"}, "qa")[0], 400)
        self.assertEqual(self.request("/api/letter/approve", {**approval, "letter_hash": "stale"}, "qa")[0], 400)
        self.assertTrue(self.request("/api/audit", role="admin")[1])
        self.assertEqual(self.request('/api/letter/' + checked['letter_hash'])[1]['state'], 'READY TO SEND')
        r['accounts'][0]['opened'] = '2026-05-13'
        self.request('/api/report', {'id': ident, 'report': r})
        self.assertEqual(self.request('/api/letter/' + checked['letter_hash'])[1]['state'], 'BLOCKED')

    def test_registry_verification_server_attribution(self):
        rel = {**relationship(), "id": "integration-registry", "inquiry_company": "OTHER", "verified_by": "forged"}
        status, saved = self.request("/api/relationship", {"record": rel, "verify": True}, "admin")
        self.assertEqual(status, 200)
        self.assertEqual(saved["verified_by"], "admin")
        self.assertEqual(self.request("/api/audit")[0], 403)

    def test_uploaded_document_workflow_and_full_letter_gate(self):
        r = report()
        def doc(content, filename, kind):
            return self.request('/api/document', {'filename': filename, 'kind': kind,
                'content_base64': base64.b64encode(content.encode()).decode()})
        status, source = doc(json.dumps(r), 'source.json', 'source')
        self.assertEqual(status, 200)
        self.assertNotIn('content_base64', source)
        body = {'id':'upload-flow', 'report':r, 'document_id':source['id']}
        self.assertEqual(self.request('/api/report', body)[0], 400)
        status, screened = self.request('/api/report', {**body, 'source_verified':True})
        self.assertEqual(status, 200)
        self.assertEqual(screened['source_report']['_source_hash'], source['source_hash'])
        payload = review(r, []); payload['fingerprint'] = screened['fingerprint']
        self.assertEqual(self.request('/api/review', {'report_id':'upload-flow','inquiry_id':'i','review':payload}, 'qa')[0], 200)
        _, letter = doc('TransUnion\n1. JPMCB (Inquiry dated 05/10/2026)', 'letter.txt', 'letter')
        letter_body = {'report_id':'upload-flow', 'text':'bureau,name,date\nTU,JPMCB,2026-05-10', 'document_id':letter['id']}
        _, checked = self.request('/api/letter/check', letter_body)
        approval = {**letter_body, 'letter_hash':checked['letter_hash'],'fingerprint':checked['fingerprint'],'approval_evidence':'Full original document reviewed'}
        self.assertEqual(self.request('/api/letter/approve', approval, 'qa')[0], 400)
        _, changed_letter = doc('Changed original letter\nTransUnion\n1. JPMCB (Inquiry dated 05/10/2026)', 'changed-letter.txt', 'letter')
        self.assertEqual(self.request('/api/letter/approve', {**approval,'document_id':changed_letter['id'],'document_verified':True}, 'qa')[0], 400)
        self.assertEqual(self.request('/api/letter/approve', {**approval,'document_verified':True}, 'qa')[1]['state'], 'READY TO SEND')
        original = self.request('/api/document/'+letter['id'])[1]
        self.assertEqual(base64.b64decode(original['content_base64']).decode(), 'TransUnion\n1. JPMCB (Inquiry dated 05/10/2026)')
        self.assertEqual(self.request('/api/letter/'+checked['letter_hash'])[1]['state'], 'READY TO SEND')
        self.assertTrue(self.request('/api/activity/upload-flow')[1])

    def test_scanned_document_blocks_approval_even_with_manual_schedule(self):
        from test_documents import pdf_with_text
        status, letter = self.request('/api/document', {'filename':'scan.pdf','kind':'letter',
            'content_base64':base64.b64encode(pdf_with_text()).decode()})
        self.assertEqual(status, 200)
        r = report()
        _, screened = self.request('/api/report', {'id':'scan-flow','report':r})
        payload = review(r, []); payload['fingerprint'] = screened['fingerprint']
        self.request('/api/review', {'report_id':'scan-flow','inquiry_id':'i','review':payload}, 'qa')
        status, checked = self.request('/api/letter/check', {'report_id':'scan-flow','document_id':letter['id'],
            'text':'bureau,name,date\nTU,JPMCB,2026-05-10'})
        self.assertEqual(checked['state'], 'BLOCKED')
        self.assertIn('unreadable', checked['blocking_document_issue'])

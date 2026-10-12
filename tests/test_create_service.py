import hashlib
import importlib.util
import io
import json
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

MODULE = Path(__file__).resolve().parent.parent/'deploy/create_service.py'
spec = importlib.util.spec_from_file_location('quality_checker_deploy',MODULE)
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)


class CreateServiceTests(unittest.TestCase):
    def test_foreign_image_is_rejected(self):
        with self.assertRaises(ValueError):
            helper.configuration('another-registry/image:latest')

    def test_missing_approval_never_attempts_authentication(self):
        with patch.object(helper.subprocess,'run') as auth:
            with self.assertRaises(ValueError):helper.create(helper.IMAGE_PREFIX+'app:test','unapproved')
            auth.assert_not_called()

    def test_create_is_post_only_and_existing_service_is_not_updated(self):
        digest=hashlib.sha256(MODULE.with_name('plan.json').read_bytes()).hexdigest()
        with patch.object(helper.subprocess,'run') as auth, patch.object(helper.urllib.request,'build_opener') as build:
            auth.return_value.returncode=0
            auth.return_value.stdout='test-only-operator-token'
            build.return_value.open.side_effect=HTTPError('https://run.googleapis.com',409,'Conflict',{},io.BytesIO())
            with self.assertRaisesRegex(RuntimeError,'already exists'):helper.create(helper.IMAGE_PREFIX+'app:test',digest)
            self.assertEqual(build.return_value.open.call_count,1)
            req=build.return_value.open.call_args.args[0]
            self.assertEqual(req.get_method(),'POST')
            self.assertEqual(req.full_url,'https://run.googleapis.com/v2/projects/quality-checker-511418/locations/us-central1/services?serviceId=quality-checker')
            body=json.loads(req.data)
            self.assertEqual(body['template']['serviceAccount'],'quality-checker-runtime@quality-checker-511418.iam.gserviceaccount.com')
            self.assertFalse(body.get('invokerIamDisabled',False))

"""Approval/isolation contracts exercised without Google calls or real receipts."""
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROJECT = {'projectId':'quality-checker-511418','name':'Quality Checker','projectNumber':'12345',
           'createTime':'2000-01-01T00:00:00Z','lifecycleState':'ACTIVE'}


class DeploymentTests(unittest.TestCase):
    def run_script(self, mode='fresh', action='--deploy', approve=True, receipt=True, extra=()):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'deploy').mkdir()
            for name in ['cloud-run.sh','plan.json']:
                shutil.copyfile(ROOT/'deploy'/name, root/'deploy'/name)
            if receipt:
                (root/'.local').mkdir()
                (root/'.local/quality-checker-project-receipt.json').write_text(json.dumps(PROJECT))
            (root/'deploy/create_service.py').write_text("import sys; print('create-only service fixture: '+sys.argv[1])\n")
            log = root/'commands.jsonl'
            cli = root/'gcloud'
            cli.write_text('''#!/usr/bin/env python3
import json, os, sys
args=sys.argv[1:]
with open(os.environ['CLI_LOG'],'a') as output: output.write(json.dumps(args)+'\\n')
mode=os.environ['CLI_MODE']
if args[:2]==['projects','create'] and mode=='existing-project':
 print('ALREADY_EXISTS',file=sys.stderr);sys.exit(1)
if args[:2] in (['projects','create'],['projects','describe']):
 p=json.loads(os.environ['PROJECT_FIXTURE'])
 if mode=='wrong-project':p['projectId']='unrelated-project'
 print(json.dumps(p))
if args[:3]==['run','services','list']:
 if mode=='denied':print('Permission denied',file=sys.stderr);sys.exit(1)
 if mode=='collision':print('quality-checker')
if args[:3]==['run','services','describe']:print('https://example.invalid')
''')
            cli.chmod(0o700)
            env={**os.environ,'PATH':str(root)+os.pathsep+os.environ['PATH'], 'CLI_LOG':str(log),
                 'CLI_MODE':mode,'PROJECT_FIXTURE':json.dumps(PROJECT)}
            digest=hashlib.sha256((root/'deploy/plan.json').read_bytes()).hexdigest()
            args=[action]+(['--approved-plan',digest] if approve else [])+list(extra)
            result=subprocess.run(['bash',str(root/'deploy/cloud-run.sh'),*args],env=env,capture_output=True,text=True)
            calls=[json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
            saved=root/'.local/quality-checker-project-receipt.json'
            return result,calls,json.loads(saved.read_text()) if saved.exists() else None

    def test_plan_does_not_contact_google(self):
        result,calls,_=self.run_script(action='--plan',approve=False,receipt=False)
        self.assertEqual(result.returncode,0)
        self.assertEqual(calls,[])
        self.assertIn('Approval fingerprint',result.stdout)

    def test_unapproved_changes_and_arbitrary_targets_are_blocked(self):
        for action,approve,extra in [('--deploy',False,()),('another-project',False,()),('--deploy',True,('existing-secret',))]:
            result,calls,_=self.run_script(action=action,approve=approve,extra=extra)
            self.assertNotEqual(result.returncode,0)
            self.assertEqual(calls,[])

    def test_existing_project_is_not_adopted(self):
        result,calls,saved=self.run_script(mode='existing-project',action='--create-project',receipt=False)
        self.assertNotEqual(result.returncode,0)
        self.assertIsNone(saved)
        self.assertEqual(len(calls),1)
        self.assertEqual(calls[0][:3],['projects','create','quality-checker-511418'])

    def test_deployment_requires_new_project_receipt(self):
        result,calls,_=self.run_script(receipt=False)
        self.assertNotEqual(result.returncode,0)
        self.assertEqual(calls,[])

    def test_project_identity_mismatch_stops_before_mutations(self):
        result,calls,_=self.run_script(mode='wrong-project')
        self.assertNotEqual(result.returncode,0)
        self.assertEqual(len(calls),1)
        self.assertEqual(calls[0][:2],['projects','describe'])

    def test_project_creation_records_only_its_new_identity(self):
        result,calls,saved=self.run_script(action='--create-project',receipt=False)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(saved,PROJECT)
        self.assertEqual(len(calls),1)

    def test_service_collision_and_discovery_denial_stop_creation(self):
        for mode in ['collision','denied']:
            result,calls,_=self.run_script(mode)
            self.assertNotEqual(result.returncode,0)
            self.assertFalse(any('create' in call or 'deploy' in call or 'add-iam-policy-binding' in call for call in calls))

    def test_exact_new_project_service_and_resources_only(self):
        result,calls,_=self.run_script()
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn('create-only service fixture: us-central1-docker.pkg.dev/quality-checker-511418/quality-checker-images/',result.stdout)
        self.assertFalse(any(call[:2]==['run','deploy'] for call in calls))
        self.assertTrue(all('--project=quality-checker-511418' in call for call in calls))
        self.assertFalse(any('delete' in call or 'update' in call or 'remove-iam-policy-binding' in call for call in calls))
        self.assertEqual(sum(call[:3]==['run','services','list'] for call in calls),2)

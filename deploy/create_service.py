"""Create Cloud Run with POST only: an existing service produces a conflict.

No patch, replace, IAM update, or upsert operation is supported by this helper.
"""
import json
import hashlib
from pathlib import Path
import subprocess
import sys
import time
import urllib.error
import urllib.request

PROJECT = 'quality-checker-511418'
REGION = 'us-central1'
SERVICE = 'quality-checker'
ROOT = f'projects/{PROJECT}/locations/{REGION}'
IMAGE_PREFIX = f'{REGION}-docker.pkg.dev/{PROJECT}/quality-checker-images/'


def configuration(image):
    if not image.startswith(IMAGE_PREFIX) or any(c.isspace() for c in image):
        raise ValueError('Image must belong to the new Quality Checker registry')
    env = {'HOST':'0.0.0.0','CQC_STORAGE':'google','CQC_PROJECT':PROJECT,
           'CQC_BUCKET':f'{PROJECT}-documents','CQC_DATABASE':'quality-checker-data','CQC_NAMESPACE':SERVICE}
    return {'name':f'{ROOT}/services/{SERVICE}','ingress':'INGRESS_TRAFFIC_ALL','labels':{'app':SERVICE},
            'template':{'serviceAccount':f'quality-checker-runtime@{PROJECT}.iam.gserviceaccount.com',
                        'scaling':{'maxInstanceCount':1,'minInstanceCount':0},'timeout':'120s',
                        'maxInstanceRequestConcurrency':1,
                        'containers':[{'image':image,'ports':[{'containerPort':8080}],
                            'resources':{'limits':{'cpu':'1','memory':'1Gi'}},
                            'env':[{'name':k,'value':v} for k,v in env.items()]+[
                                {'name':'CQC_IDENTITIES','valueSource':{'secretKeyRef':{'secret':'quality-checker-identities','version':'1'}}}],
                            'startupProbe':{'httpGet':{'path':'/health','port':8080},'timeoutSeconds':3,
                                            'periodSeconds':10,'failureThreshold':12}}]}}


class NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise RuntimeError('Google API redirect refused')


def create(image, approved_plan):
    expected = hashlib.sha256(Path(__file__).with_name("plan.json").read_bytes()).hexdigest()
    if approved_plan != expected:
        raise ValueError("Cloud creation blocked: explicit approval fingerprint required")
    body = configuration(image)
    # Operator authentication stays in memory and never appears in command output.
    auth = subprocess.run(['gcloud','auth','print-access-token',f'--project={PROJECT}'],capture_output=True,text=True)
    if auth.returncode or not auth.stdout.strip():
        raise RuntimeError('Google operator authentication unavailable')
    token = auth.stdout.strip()
    opener = urllib.request.build_opener(NoRedirects())

    def request(path, payload=None):
        if not path.startswith(ROOT + '/'):
            raise RuntimeError('Cross-project or cross-region operation refused')
        req = urllib.request.Request('https://run.googleapis.com/v2/'+path,
            data=json.dumps(payload).encode() if payload is not None else None,
            headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'})
        try:
            with opener.open(req,timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            if error.code == 409:
                raise RuntimeError('quality-checker already exists. Refusing to deploy to or overwrite it.') from None
            raise RuntimeError(f'Cloud Run creation failed with HTTP {error.code}; no update was attempted') from None

    result = request(f'{ROOT}/services?serviceId={SERVICE}',body)
    operation = result.get('name','')
    if not operation.startswith(ROOT+'/operations/'):
        raise RuntimeError('Unexpected operation identity; automatic processing stopped')
    deadline = time.monotonic()+900
    while not result.get('done'):
        if time.monotonic() >= deadline:
            raise RuntimeError('Creation still pending; inspect only the new Quality Checker project. Do not retry creation blindly.')
        time.sleep(5)
        result = request(operation)
    if result.get('error'):
        raise RuntimeError('New service creation failed. Inspect the new project; existing services were not updated.')
    service = result.get('response',{})
    if service.get('name') != f'{ROOT}/services/{SERVICE}':
        raise RuntimeError('Unexpected created service identity')
    print(service.get('uri') or 'Service created; inspect the new quality-checker service for its private URL.')


if __name__ == '__main__':
    try:
        if len(sys.argv)!=4 or sys.argv[2]!='--approved-plan':
            raise ValueError('Provide the new registry image and --approved-plan fingerprint')
        create(sys.argv[1], sys.argv[3])
    except (ValueError, RuntimeError, OSError) as error:
        raise SystemExit(str(error)) from None

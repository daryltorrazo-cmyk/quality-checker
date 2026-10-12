#!/usr/bin/env bash
# Explicit approval and a project-creation receipt are required for cloud writes.
set -euo pipefail
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
cd "$script_dir/.."
project=quality-checker-511418
region=us-central1
service=quality-checker
registry=quality-checker-images
account=quality-checker-runtime
build_account=quality-checker-build
database=quality-checker-data
bucket=quality-checker-511418-documents
build_bucket=quality-checker-511418-builds
identity_secret=quality-checker-identities
service_account="${account}@${project}.iam.gserviceaccount.com"
build_service_account="${build_account}@${project}.iam.gserviceaccount.com"
receipt=.local/quality-checker-project-receipt.json
plan_hash=$(python3 -c 'import hashlib; print(hashlib.sha256(open("deploy/plan.json","rb").read()).hexdigest())')
mode=${1:---plan}
if [[ "$mode" == --plan ]]; then
  cat deploy/plan.json
  printf '\nApproval fingerprint: %s\nNo cloud operations were performed.\n' "$plan_hash"
  exit 0
fi
if [[ "$mode" != --create-project && "$mode" != --deploy ]]; then
  echo 'Usage: bash deploy/cloud-run.sh --plan | --create-project --approved-plan HASH | --deploy --approved-plan HASH' >&2
  exit 1
fi
[[ ${2:-} == --approved-plan && ${3:-} == "$plan_hash" && $# == 3 ]] || {
  echo 'Cloud changes blocked: review --plan and provide its exact approval fingerprint.' >&2
  exit 1
}
# The allowlist is fixed. No arbitrary project, service, or secret arguments exist.
python3 - <<'PYPLAN'
import json
p=json.load(open('deploy/plan.json'))
expected={'project_name':'Quality Checker','project_id':'quality-checker-511418','region':'us-central1',
'cloud_run_service':'quality-checker','artifact_registry':'quality-checker-images','firestore_database':'quality-checker-data',
'document_bucket':'quality-checker-511418-documents','build_bucket':'quality-checker-511418-builds',
'runtime_service_account':'quality-checker-runtime','build_service_account':'quality-checker-build',
'identity_secret':'quality-checker-identities','access':'private','github_repository':'daryltorrazo-cmyk/quality-checker'}
if any(p.get(k)!=v for k,v in expected.items()): raise SystemExit('Plan differs from the fixed isolation allowlist')
expected_apis={'cloudresourcemanager.googleapis.com','serviceusage.googleapis.com','run.googleapis.com','cloudbuild.googleapis.com',
'artifactregistry.googleapis.com','firestore.googleapis.com','storage.googleapis.com','secretmanager.googleapis.com','iam.googleapis.com'}
if set(p.get('apis',[]))!=expected_apis: raise SystemExit('Unexpected APIs in deployment plan')
PYPLAN
command -v gcloud >/dev/null || { echo 'Use an authenticated Google Cloud Shell. No cloud changes performed.' >&2; exit 1; }
mkdir -p .local
if [[ "$mode" == --create-project ]]; then
  [[ ! -e "$receipt" ]] || { echo 'Project-creation receipt already exists. Refusing to create or reuse another project.' >&2; exit 1; }
  task_receipt=$(mktemp .local/new-project.XXXXXX)
  trap 'rm -f -- "$task_receipt"' EXIT
  # If the project ID exists, create fails; it is never silently adopted.
  gcloud projects create "$project" --name='Quality Checker' --project="$project" --format=json > "$task_receipt"
  python3 - "$task_receipt" "$receipt" <<'PYRECEIPT'
import json, os, sys
p=json.load(open(sys.argv[1]))
if p.get('projectId')!='quality-checker-511418' or p.get('name')!='Quality Checker' or not p.get('projectNumber') or not p.get('createTime'):
    raise SystemExit('Project creation could not be verified; deployment remains blocked')
with open(sys.argv[2],'x') as output: json.dump(p,output)
os.chmod(sys.argv[2],0o600)
PYRECEIPT
  echo 'New project created. Link billing for this new project in Google Cloud Console, then review and approve deployment.'
  exit 0
fi
[[ -f "$receipt" ]] || { echo 'No verified new-project receipt. Refusing to deploy into a pre-existing project.' >&2; exit 1; }
project_state=$(mktemp .local/project-state.XXXXXX)
trap 'rm -f -- "$project_state"' EXIT
gcloud projects describe "$project" --project="$project" --format=json > "$project_state"
python3 - "$receipt" "$project_state" <<'PYVERIFY'
import json, sys
created=json.load(open(sys.argv[1])); live=json.load(open(sys.argv[2]))
for key in ['projectId','projectNumber','createTime','name']:
    if created.get(key)!=live.get(key): raise SystemExit('Project creation identity changed; deployment blocked')
if live.get('lifecycleState')!='ACTIVE': raise SystemExit('New project is not active')
PYVERIFY
rm -f -- "$project_state"
trap - EXIT
apis=(cloudresourcemanager.googleapis.com serviceusage.googleapis.com run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com firestore.googleapis.com storage.googleapis.com secretmanager.googleapis.com iam.googleapis.com)
gcloud services enable "${apis[@]}" --project="$project"
# Discovery errors stop execution. Name collisions are never overwritten or reused.
check_absent() {
  local kind=$1 name=$2 existing
  shift 2
  existing=$("$@")
  if printf '%s\n' "$existing" | grep -Fxq -- "$name"; then
    echo "Refusing to overwrite or reuse existing ${kind}: ${name}" >&2
    exit 1
  fi
}
check_absent service "$service" gcloud run services list --region="$region" --project="$project" --format='value(metadata.name)'
check_absent registry "$registry" gcloud artifacts repositories list --location="$region" --project="$project" --format='value(name.basename())'
check_absent runtime-account "$service_account" gcloud iam service-accounts list --project="$project" --format='value(email)'
check_absent build-account "$build_service_account" gcloud iam service-accounts list --project="$project" --format='value(email)'
check_absent database "$database" gcloud firestore databases list --project="$project" --format='value(name.basename())'
check_absent document-bucket "$bucket" gcloud storage buckets list --project="$project" --format='value(name)'
check_absent build-bucket "$build_bucket" gcloud storage buckets list --project="$project" --format='value(name)'
check_absent secret "$identity_secret" gcloud secrets list --project="$project" --format='value(name.basename())'
gcloud artifacts repositories create "$registry" --repository-format=docker --location="$region" --project="$project"
gcloud iam service-accounts create "$account" --display-name='Quality Checker runtime' --project="$project"
gcloud iam service-accounts create "$build_account" --display-name='Quality Checker build' --project="$project"
gcloud firestore databases create --database="$database" --location="$region" --type=firestore-native --project="$project"
gcloud storage buckets create "gs://${bucket}" --location="$region" --uniform-bucket-level-access --public-access-prevention --project="$project"
gcloud storage buckets create "gs://${build_bucket}" --location="$region" --uniform-bucket-level-access --public-access-prevention --project="$project"
gcloud projects add-iam-policy-binding "$project" --member="serviceAccount:${build_service_account}" --role=roles/cloudbuild.builds.builder --condition=None --project="$project" >/dev/null
gcloud storage buckets add-iam-policy-binding "gs://${build_bucket}" --member="serviceAccount:${build_service_account}" --role=roles/storage.objectAdmin --project="$project" >/dev/null
gcloud projects add-iam-policy-binding "$project" --member="serviceAccount:${service_account}" --role=roles/datastore.user --condition=None --project="$project" >/dev/null
gcloud storage buckets add-iam-policy-binding "gs://${bucket}" --member="serviceAccount:${service_account}" --role=roles/storage.objectCreator --project="$project" >/dev/null
gcloud storage buckets add-iam-policy-binding "gs://${bucket}" --member="serviceAccount:${service_account}" --role=roles/storage.objectViewer --project="$project" >/dev/null
task_secret_dir=$(mktemp -d "${TMPDIR:-/tmp}/quality-checker-identities.XXXXXX")
trap 'rm -rf -- "$task_secret_dir"' EXIT
python3 - "$task_secret_dir/identities.json" <<'PYIDENTITIES'
import json, os, secrets, sys
with open(sys.argv[1],'w') as output:
    json.dump({name:{'role':role,'token':secrets.token_urlsafe(48)} for name,role in
               [('bootstrap-agent','agent'),('bootstrap-reviewer','qa'),('bootstrap-admin','admin')]},output)
os.chmod(sys.argv[1],0o600)
PYIDENTITIES
gcloud secrets create "$identity_secret" --replication-policy=automatic --data-file="$task_secret_dir/identities.json" --project="$project"
rm -rf -- "$task_secret_dir"
trap - EXIT
gcloud secrets add-iam-policy-binding "$identity_secret" --member="serviceAccount:${service_account}" --role=roles/secretmanager.secretAccessor --project="$project" >/dev/null
image="${region}-docker.pkg.dev/${project}/${registry}/app:$(date -u +%Y%m%d%H%M%S)"
gcloud builds submit --tag="$image" --project="$project" --service-account="projects/${project}/serviceAccounts/${build_service_account}" --gcs-source-staging-dir="gs://${build_bucket}/source" --gcs-log-dir="gs://${build_bucket}/logs" .
# Check again immediately before deployment, including after a potentially lengthy build.
check_absent service "$service" gcloud run services list --region="$region" --project="$project" --format='value(metadata.name)'
# A create-only POST rejects a concurrently created service atomically.
python3 deploy/create_service.py "$image" --approved-plan "$plan_hash"

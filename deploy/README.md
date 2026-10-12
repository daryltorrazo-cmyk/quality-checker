# Quality Checker deployment — approval required

Only the brand-new repository `daryltorrazo-cmyk/quality-checker` and the new Google
Cloud project **Quality Checker**, ID `quality-checker-511418`, are in scope.
The new service must be `quality-checker` in `us-central1`.

## Review before any cloud writes

```sh
bash deploy/cloud-run.sh --plan
```

This prints `plan.json` and its SHA-256 fingerprint without contacting Google Cloud.
The plan lists nine APIs and all new resources. API activation may also create
Google-managed service agents inside this new project. No domains, Sheets,
schedulers, triggers, or external systems are connected.

After explicit approval and Google authentication, a project creation step uses:

```sh
bash deploy/cloud-run.sh --create-project --approved-plan APPROVED_FINGERPRINT
```

An existing project ID causes creation to fail. It is never silently reused. The
script records the identity of the new project in an ignored local receipt. If the
ID already exists, stop and ask the user how to proceed; never delete it, adopt it,
or inspect another project as a workaround. Link billing only for this new project
through Google Cloud Console after the user's authorization; no existing project's
billing configuration is modified by this script.

Once billing and approval are confirmed, deployment uses the same checkout/receipt:

```sh
bash deploy/cloud-run.sh --deploy --approved-plan APPROVED_FINGERPRINT
```

All commands target `quality-checker-511418` explicitly. No arbitrary project,
region, service, database, or existing secret parameters are accepted. Cloud Run,
registry, Firestore, buckets, service accounts, and secret collisions stop the
script. The service name is checked again after building. Cloud Run uses a create-only POST, never a deployment upsert: even a concurrent
service-name collision is rejected atomically.

The runtime and build accounts, image registry, named Firestore database, document
and build buckets, and reviewer secret are all newly created. The app stays private
behind Google IAM. Reviewer identities are generated securely in the new Secret
Manager secret; their values are never printed or committed. The frontend passes
`X-CQC-Token` separately from Google IAM authorization. Replace bootstrap identities
with individual accounts before real client use.

Large records are stored as immutable Cloud Storage objects; Firestore holds
checksummed pointers and transactional audit/lease records. Failed or expired
review locks cannot approve letters. No SQLite data or credentials are migrated.
Containers run as a non-root user on `0.0.0.0:$PORT`. Cloud resources may incur charges.

Local tests use in-memory SDK/CLI fakes; no live Google resources are accessed.
Google authentication is currently unavailable in this workspace. Actual Cloud
Build, cloud persistence, and the deployed app still require live validation after
approval. The local Docker network cannot resolve PyPI; container validation can
use verified, downloaded wheels via an offline installation variant.

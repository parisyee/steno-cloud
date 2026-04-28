# Infrastructure

Source-of-truth document for everything cloud-side that backs Steno. Until the project moves to IaC (Terraform / Pulumi), this file is the runbook — anyone (or any agent) standing up a new environment should be able to follow it top-to-bottom and end up with a working stack.

> **Scope:** this file covers the **server-side** infra used by `steno-cloud`. Per-client setup (Apple Developer Program for iOS, Vercel deploy for `steno-nextjs` if it gets one) is documented inside those repos.

---

## 1. System map

```
┌─────────────────────┐          ┌─────────────────────────┐
│ steno-ios           │          │ steno-nextjs            │
│ (Voice Memos share) │          │ (browser uploads)       │
└──────────┬──────────┘          └────────────┬────────────┘
           │                                  │
           │  HTTPS, Bearer STENO_API_KEY     │  /api/* proxy on Next.js
           │  (HTTP/2 end-to-end, multipart)  │  (Bearer added server-side)
           │                                  │
           └────────────────┬─────────────────┘
                            ▼
            ┌──────────────────────────────────┐
            │ Cloud Run service: steno         │
            │ region: us-central1              │
            │ project: steno-prod              │
            │ image: us-central1-docker.pkg... │
            │   .dev/steno-prod/steno/api      │
            │ runtime: Hypercorn (h2c)         │
            └─────────┬─────────────────┬──────┘
                      │                 │
                      ▼                 ▼
        ┌────────────────────┐   ┌──────────────────────────┐
        │ Gemini API         │   │ Supabase (Postgres)      │
        │ (auth via          │   │ project: steno-prod      │
        │  GEMINI_API_KEY)   │   │ table: transcriptions    │
        └────────────────────┘   │ extensions: unaccent     │
                                 │ (pgvector planned)       │
                                 └──────────────────────────┘
                            ▲
                            │ supabase db push
                            │ (CLI on dev machine,
                            │  not run from CI today)
                            │
                ┌─────────────────────────┐
                │ GitHub: parisyee/steno- │
                │ {cloud,ios,nextjs}      │
                │ Actions deploys cloud   │
                └─────────────────────────┘
```

### Hosted vendors and what they store

| Vendor | Resource | What it holds | Cost model |
|---|---|---|---|
| **Google Cloud** | Cloud Run service `steno` | Stateless container; pulls image from Artifact Registry on each cold start | Pay-per-request; generous free tier |
| **Google Cloud** | Artifact Registry repo `steno` | Docker images, one tag per git SHA + `latest` | Storage by GB-month |
| **Google Cloud** | IAM service account `github-deployer` | Identity GitHub Actions assumes to deploy | Free |
| **Supabase** | Postgres database | All transcriptions and the full-text search index | Free tier covers ≤500 MB |
| **Google AI Studio** | Gemini API key | Used by the API to call Gemini 2.5 Pro / Flash | Pay-per-token |
| **GitHub** | Actions secrets on `parisyee/steno-cloud` | Deploy-time secrets injected as Cloud Run env vars | Free for public repos |

---

## 2. Naming conventions and parameters

When standing up a new environment (e.g. `steno-staging`), substitute these across the rest of this document:

| Placeholder | Prod value | Notes |
|---|---|---|
| `<PROJECT_ID>` | `steno-prod` | GCP project ID. Must be globally unique. |
| `<REGION>` | `us-central1` | Pick once; everything (Cloud Run, Artifact Registry) uses the same region. |
| `<SERVICE_NAME>` | `steno` | Cloud Run service name. |
| `<AR_REPO>` | `steno` | Artifact Registry repo name. |
| `<SA_NAME>` | `github-deployer` | Service account that GitHub Actions impersonates. |
| `<SUPABASE_PROJECT>` | `steno-prod` | Supabase project name. The project ref (in URLs) is generated, not chosen. |
| `<GH_REPO>` | `parisyee/steno-cloud` | GitHub repo whose Actions deploy this stack. |

The GitHub Actions workflow file (`.github/workflows/deploy.yml`) hardcodes prod values today. To support multiple envs, either fork the workflow per-branch or move these into repo-level Actions variables.

---

## 3. Provisioning a new environment

These steps assume a fresh GCP account with billing enabled and the `gcloud` CLI authenticated. Run from anywhere; no local code required until step 4.

### 3.1 Create the GCP project

```bash
PROJECT_ID=steno-prod
REGION=us-central1
SERVICE_NAME=steno
AR_REPO=steno
SA_NAME=github-deployer

gcloud projects create "$PROJECT_ID" --name="Steno"
gcloud config set project "$PROJECT_ID"

# Link a billing account (required to enable most APIs)
gcloud beta billing projects link "$PROJECT_ID" \
  --billing-account=YOUR_BILLING_ACCOUNT_ID
```

### 3.2 Enable required APIs

```bash
gcloud services enable \
  run.googleapis.com \
  artifactregistry.googleapis.com \
  cloudbuild.googleapis.com \
  iam.googleapis.com \
  iamcredentials.googleapis.com
```

| API | Why |
|---|---|
| `run.googleapis.com` | The Cloud Run service itself |
| `artifactregistry.googleapis.com` | Stores Docker images |
| `cloudbuild.googleapis.com` | Used implicitly by `gcloud run deploy --source` (not strictly required when CI builds the image, but harmless to enable) |
| `iam.googleapis.com` | Service account creation |
| `iamcredentials.googleapis.com` | Token minting for the deploy SA (needed by `google-github-actions/auth`) |

### 3.3 Create the Artifact Registry repo

```bash
gcloud artifacts repositories create "$AR_REPO" \
  --repository-format=docker \
  --location="$REGION" \
  --description="Steno container images"
```

This produces the registry URL the deploy workflow pushes to:

```
$REGION-docker.pkg.dev/$PROJECT_ID/$AR_REPO/api
```

### 3.4 Create the deploy service account and grant roles

```bash
gcloud iam service-accounts create "$SA_NAME" \
  --display-name="GitHub Actions Deployer"

SA_EMAIL="$SA_NAME@$PROJECT_ID.iam.gserviceaccount.com"

# 1) Deploy / update the Cloud Run service
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:$SA_EMAIL" \
  --role="roles/run.admin"

# 2) Push images to Artifact Registry
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:$SA_EMAIL" \
  --role="roles/artifactregistry.writer"

# 3) Required so run.admin can attach the runtime SA to the service
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:$SA_EMAIL" \
  --role="roles/iam.serviceAccountUser"
```

#### Why these three roles, and nothing more

| Role | Granted on | Purpose | Why it's needed |
|---|---|---|---|
| `roles/run.admin` | project | Create / update Cloud Run services and revisions | The workflow's last step is `gcloud run deploy`. |
| `roles/artifactregistry.writer` | project | Push images to any Artifact Registry repo in the project | The workflow does `docker push` before deploy. Read is implicit. |
| `roles/iam.serviceAccountUser` | project | Allow this SA to "act as" any service account | Cloud Run revisions run *as* a service account (today: the default Compute SA). To attach that runtime SA to a new revision, the deployer needs `actAs` on it. Without this you get `iam.serviceaccounts.actAs` denied at deploy time. |

We deliberately **do not** grant `roles/owner`, `roles/editor`, or `roles/serviceAccountTokenCreator`. The deployer SA can build, push, and deploy — nothing else.

### 3.5 Generate the deploy SA key

```bash
gcloud iam service-accounts keys create ~/steno-deployer-key.json \
  --iam-account="$SA_EMAIL"
```

The contents of `~/steno-deployer-key.json` go into the GitHub secret `GCP_SA_KEY` (see §5). Treat this file like a password — rotate it (`keys list` → `keys delete`) on a regular cadence; anyone with it can deploy on your behalf.

> **Future hardening:** swap the JSON key for [Workload Identity Federation](https://github.com/google-github-actions/auth#preferred-direct-workload-identity-federation) so GitHub OIDC can mint short-lived tokens, no long-lived key required. Tracked but not yet adopted.

### 3.6 Create the Supabase project

1. In the Supabase dashboard, create a new project named `<SUPABASE_PROJECT>`.
2. From **Settings → API**, copy:
   - **Project URL** — goes in `SUPABASE_URL`
   - **`service_role` key** — goes in `SUPABASE_KEY`
   The `anon` key is *not* used; the API authenticates server-side as service role.
3. From **Settings → Database**, note the database password — needed for `supabase db push`.

### 3.7 Apply database migrations

From a checkout of `steno-cloud`:

```bash
brew install supabase/tap/supabase
supabase link --project-ref <ref-from-supabase-url>
supabase db push
```

`supabase/migrations/` is the source of truth for the schema. Migrations run today (in order):

| File | Adds |
|---|---|
| `001_init.sql` | `transcriptions` table, `tsvector` search column + GIN index, `unaccent` extension |
| `002_title_description_cleaned.sql` | `title`, `description`, `cleaned_polished` columns; expanded search vector |
| `003_drop_cleaned_light.sql` | Removes the abandoned `cleaned_light` experiment column |
| `004_transcription_attempts.sql` | Per-attempt diagnostics table for failed/partial uploads |

Running `supabase db push` from CI is **not** wired up today — migrations are applied manually. That's a known gap; see §7.

### 3.8 Provision the Gemini API key

[Google AI Studio → Get API key](https://aistudio.google.com/apikey) → copy the key. Goes into `GEMINI_API_KEY` everywhere it's referenced in §5.

### 3.9 Mint the Steno API key

This is the bearer token that gates the Cloud Run service:

```bash
openssl rand -hex 32
```

Put the value in the GitHub secret `STENO_API_KEY`, the corresponding env vars on `steno-nextjs` (Vercel / hosting) and `steno-ios` (`Shared/Secrets.swift`). Keep it out of git.

### 3.10 First deploy

Push to `main`. The workflow in §4 builds the image, pushes it, and creates the Cloud Run revision. The first deploy is a "create" (no existing service), every deploy after is an in-place update with zero-downtime traffic shift.

The Cloud Run URL is auto-generated and stable across revisions. Once issued (e.g. `https://steno-XXXXXXXXX-uc.a.run.app`), record it in:
- `steno-nextjs` env var `STENO_API_URL`
- `steno-ios` `Shared/Config.swift` (`apiBaseURL`)

---

## 4. Cloud Run service configuration

Today's service runs with these flags (see `.github/workflows/deploy.yml`):

| Flag | Value | Why |
|---|---|---|
| `--platform managed` | — | Fully managed Cloud Run (not Anthos). |
| `--region us-central1` | — | Cheap, generous free tier, low GCS/Gemini latency. |
| `--allow-unauthenticated` | — | Public ingress; auth is enforced *inside* the app via `STENO_API_KEY`. |
| `--use-http2` | — | HTTP/2 end-to-end. Lifts the 32 MB request body cap on the HTTP/1.1 forwarding path so we can accept multi-GB uploads. The container must speak h2c — that's why we run Hypercorn instead of uvicorn. |
| `--memory 4Gi` | — | Headroom for ffmpeg silence-trim of multi-hour audio. |
| `--timeout 3600` | — | Max Cloud Run timeout (1 hour). Long uploads + Gemini transcription can run minutes. |
| `--set-env-vars` | (5 secrets) | See §5. Injected at deploy time, never baked into the image. |

Defaults we rely on: 1 vCPU, autoscaling 0–100 instances, concurrency 80, no VPC connector (Supabase is reached over the public internet). Cold starts run ~3–5 s including image pull.

### Runtime service account

Cloud Run revisions execute as the **default Compute Engine service account** (`<PROJECT_NUMBER>-compute@developer.gserviceaccount.com`) because we never set `--service-account`. This SA has `roles/editor` by default (project-wide). For a stricter setup, create a dedicated runtime SA with no project-level roles and pass it via `--service-account` — the app doesn't need any GCP-side permissions, since Gemini and Supabase auth are over-the-wire keys.

---

## 5. Secrets matrix

Where each secret lives and how it gets to the running service. Everything below is per-environment.

| Secret | Source of truth | Stored in | Consumed by |
|---|---|---|---|
| `GCP_SA_KEY` | `gcloud iam service-accounts keys create` | GitHub Actions secret on `<GH_REPO>` | The `auth` step of the deploy workflow |
| `GEMINI_API_KEY` | Google AI Studio | GitHub Actions secret + local `.env` | Cloud Run env var; read by `transcription_service/gemini_client.py` |
| `SUPABASE_URL` | Supabase dashboard → Settings → API | GitHub Actions secret + local `.env` | Cloud Run env var; read by `api/deps.py` |
| `SUPABASE_KEY` | Supabase dashboard → Settings → API (`service_role`) | GitHub Actions secret + local `.env` | Cloud Run env var; read by `api/deps.py` |
| `STENO_API_KEY` | `openssl rand -hex 32` (mint once) | GitHub Actions secret + local `.env` + `steno-nextjs` env + `steno-ios/Shared/Secrets.swift` | Cloud Run env var (gates the API); proxy auth header on Next.js; bearer on iOS |

GitHub UI: `Settings → Secrets and variables → Actions → Repository secrets`.

The local `.env` is gitignored (`.gitignore` covers `.env`); a checked-in `.env.example` lists the variable names so a fresh clone knows what to fill in.

---

## 6. CI/CD — what GitHub Actions does on every push to `main`

`.github/workflows/deploy.yml` runs five steps. Roughly:

```
checkout → auth to GCP → setup gcloud → docker build/push → gcloud run deploy
```

Step-by-step responsibilities:

1. **`actions/checkout@v4`** — pull the repo at the pushed commit.
2. **`google-github-actions/auth@v2`** — read the `GCP_SA_KEY` secret, write it to a temp file, and export `GOOGLE_APPLICATION_CREDENTIALS` so subsequent `gcloud` calls authenticate as `github-deployer`.
3. **`setup-gcloud@v2`** — install `gcloud` on the runner.
4. **`gcloud auth configure-docker`** — register Artifact Registry as a Docker credential helper, so `docker push` to `us-central1-docker.pkg.dev` works.
5. **Build + push** — build the Dockerfile, tag with both `:<sha>` and `:latest`, push both.
6. **`gcloud run deploy`** — point the service at the new SHA-tagged image and inject the four runtime env vars from secrets. Cloud Run shifts traffic to the new revision once it passes its health check.

Failures at any step leave the previously running revision untouched. There is no rollback automation today — to roll back, push a revert commit (or use `gcloud run services update-traffic`).

---

## 7. Known gaps / TODOs

These are not blockers for current operation but matter for reproducibility:

1. **Migrations are run manually.** `supabase db push` only happens from a developer's machine. A new environment requires a human to run it once. → Wire migrations into a separate CI job that runs *before* the Cloud Run deploy on schema-touching commits.
2. **Workflow is hardcoded to `steno-prod`.** Standing up `steno-staging` requires either editing the workflow or templating the env vars at the top of `deploy.yml` into Actions variables.
3. **No IaC.** Everything in §3 is imperative `gcloud` commands. A Terraform module would let us tear down and recreate environments. The role list in §3.4 is a good seed.
4. **Long-lived SA key.** `GCP_SA_KEY` is a JSON key on disk. Workload Identity Federation would replace it with short-lived OIDC-issued tokens.
5. **Runtime SA is the default Compute SA.** Has more permissions than needed. A dedicated zero-permission SA passed via `--service-account` would be tighter.
6. **No structured backups.** Supabase free tier handles point-in-time recovery up to 7 days. A scheduled `pg_dump` to GCS would be a cheap belt-and-suspenders.

---

## 8. Quick reference — common operations

### Roll back the API to a previous revision

```bash
gcloud run revisions list --service steno --region us-central1
gcloud run services update-traffic steno \
  --region us-central1 \
  --to-revisions <REVISION_NAME>=100
```

### Tail Cloud Run logs

```bash
gcloud logging tail "resource.type=cloud_run_revision AND resource.labels.service_name=steno" \
  --project steno-prod
```

### Inspect the running env vars

```bash
gcloud run services describe steno --region us-central1 \
  --format="value(spec.template.spec.containers[0].env)"
```

### Rotate the Steno API key

1. `openssl rand -hex 32`
2. Update the secret in GitHub Actions, the Vercel/hosting env for `steno-nextjs`, and `steno-ios/Shared/Secrets.swift`.
3. Push to `main` (or trigger a manual deploy) — the Cloud Run revision picks up the new key. The old key works until the new revision finishes rolling out.

### Rotate the deploy SA key

```bash
# List existing keys
gcloud iam service-accounts keys list --iam-account="$SA_EMAIL"

# Mint a new one and rotate the GitHub secret
gcloud iam service-accounts keys create ~/new-key.json --iam-account="$SA_EMAIL"

# After confirming a deploy succeeded with the new key, delete the old one
gcloud iam service-accounts keys delete <OLD_KEY_ID> --iam-account="$SA_EMAIL"
```

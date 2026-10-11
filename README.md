# Micro-Grid Community Energy Trading & Resiliency Agent

A runnable full-stack hackathon prototype: four autonomous household agents submit
structured energy bids, and a community coordinator shares solar/battery energy
and sheds flexible loads during disruptions. A React dashboard displays the
neighborhood network, battery/solar metrics, and inspectable JSON negotiation logs.
FastAPI serves the simulation; Google Gemini supplies optional agent decisions.

### Why this matters in storms and outages

This project is designed to keep working when the grid is under stress.
During a storm, solar generation drops, demand becomes harder to balance, and some
homes may lose reliable supply. The community coordinator still reallocates energy
across households, prioritizes critical loads, and uses battery reserves to keep the
micro-grid functional. In an outage, the system islands the neighborhood and continues
coordinating energy locally instead of depending on the utility grid.

**How to explain this to the jury:**
- Even when solar production falls, the coordinator adapts.
- Critical loads are protected first.
- Battery energy is shared where it helps most.
- The system continues making decisions locally during grid disruptions.
- The dashboard shows that resilience is not theoretical — it is simulated live.

## Run locally (no cloud account or AI key required)

Requirements: Python 3.12+, Node.js 22.12+ (or 24+), npm. Run these commands from
the repository root:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r backend/requirements-dev.txt
python -m uvicorn backend.main:app --reload --host 127.0.0.1 --port 8000
```

In a second terminal:

```bash
cd frontend
npm ci
npm run dev -- --host 127.0.0.1
```

Open **http://localhost:5173**. The Vite development proxy forwards `/api` to
FastAPI; interactive API documentation is at **http://localhost:8000/docs**.
Click **Simulate storm**, **Simulate grid outage**, or **Reset community**.
The dashboard polls every three seconds; simulations advance only when triggered.
Each event advances a 15-minute interval, and successive rounds retain battery
changes. Reset restores starting conditions and clears previous logs.

### Containers

With Docker Engine and Docker Compose:

```bash
docker compose up --build
```

Open **http://localhost:8080**. Compose uses deterministic agents and memory state
by default. Both Dockerfiles use the repository root as their build context.
The frontend nginx proxy reads `BACKEND_URL` at container startup, so the same
image works locally and on Cloud Run without baking credentials into JavaScript.

## Enable Gemini

Configuration is read from process environment variables. `.env` files are **not**
automatically loaded; `backend/.env.example` documents the available settings.

For the Gemini Developer API:

```bash
read -rsp 'Gemini API key: ' GEMINI_API_KEY; echo
export GEMINI_API_KEY
export GEMINI_MODEL=gemini-2.5-flash
python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

For Vertex AI using Application Default Credentials:

```bash
gcloud auth application-default login
export GOOGLE_CLOUD_PROJECT=your-project-id
export GOOGLE_GENAI_USE_VERTEXAI=true
export GOOGLE_CLOUD_LOCATION=global
export GEMINI_MODEL=gemini-2.5-flash
python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

Enable `aiplatform.googleapis.com` and grant the runtime identity
`roles/aiplatform.user`. Use a model/location available to your project.
Households call the Google GenAI SDK asynchronously with a Pydantic JSON response
schema. Missing credentials, timeouts, or invalid responses produce explicit
`deterministic` fallback entries rather than fabricated Gemini output. Default AI
timeout is five seconds per household; `BACKEND_AI_TIMEOUT_SECONDS` accepts
0.1–30 seconds. Household calls run concurrently.

## State and API

| Setting | Default | Purpose |
| --- | --- | --- |
| `BACKEND_STORE` | `memory` | `memory` or `firestore` |
| `GOOGLE_CLOUD_PROJECT` | empty | Required for Firestore/Vertex |
| `FIRESTORE_COLLECTION` | `microgrid` | Firestore collection |
| `FIRESTORE_DOCUMENT` | `simulation` | Shared simulation document |
| `FIRESTORE_EMULATOR_HOST` | unset | Emulator host:port, e.g. `127.0.0.1:8085` |
| `BIGQUERY_TABLE` | empty | Optional fully qualified `project.dataset.table` for committed snapshot rows |
| `GCS_BUCKET` | empty | Optional bucket for snapshot archives and Vision image inputs |
| `PUBSUB_TOPIC` | empty | Optional Pub/Sub topic ID for committed dispatch events |
| `EARTH_ENGINE_PROJECT` | empty | Optional registered Earth Engine project for solar context |
| `BACKEND_API_TOKEN` | empty | Optional local mutation token |
| `BACKEND_PRODUCTION` | `false` | Requires a mutation token at startup |
| `BACKEND_CORS_ORIGINS` | `http://localhost:5173,http://localhost:3000` | Comma-separated allowed origins |

Memory mode is explicitly selected by default and is lost when the process
restarts; use one backend worker in this mode. Firestore mode uses
`google.cloud.firestore.AsyncClient`, persists its document on the first read,
and atomically compares revisions before writing the state and ledger together.
Conflicting rounds return HTTP 409; retry manually. Firestore errors return 503,
not a silent switch to memory. Logs are bounded to the newest 200 entries.

For an already-running Firestore emulator:

```bash
export BACKEND_STORE=firestore
export GOOGLE_CLOUD_PROJECT=demo-micro-grid
export FIRESTORE_EMULATOR_HOST=127.0.0.1:8085
python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

For real Firestore, unset `FIRESTORE_EMULATOR_HOST`, use Application Default
Credentials, create a Native mode `(default)` database, and grant the runtime
identity `roles/datastore.user`.

| Endpoint | Behavior |
| --- | --- |
| `GET /health` | State store readiness |
| `GET /api/state` | Current households, totals, trades, and revision |
| `GET /api/logs` | Structured decisions and coordinator ledger |
| `POST /api/disruptions` | Body `{"event":"storm"}` or `{"event":"outage"}` |
| `POST /api/reset` | Restore initial simulation |
| `POST /api/images/analyze` | Body `{"gcs_uri":"gs://bucket/object"}`; Cloud Vision labels an image from the configured bucket |
| `GET /api/geospatial/solar?latitude=37.4&longitude=-122.1` | Optional Earth Engine average hourly irradiance context for the previous seven days |

When configured, protected API operations require `X-API-Key` matching
`BACKEND_API_TOKEN`. This includes image analysis and Earth Engine requests. Enter
the token in the dashboard operator field; it stays in memory only.
Read endpoints remain public. Never put secrets in `VITE_*` variables.

## Deploy to Google Cloud Run

Requirements: Google Cloud CLI, authenticated deployment identity, a
billing-enabled project, and permissions to enable APIs, manage service accounts,
IAM, Artifact Registry, Firestore, Cloud Build, Cloud Run, and Secret Manager.
Cloud Build's build identity also needs Artifact Registry writer and logging
permissions. Deployment creates billable resources; configure budgets.

Create an operator token without committing it or placing it in command history:

```bash
export GOOGLE_CLOUD_PROJECT=your-project-id
export REGION=us-central1
export API_TOKEN_SECRET=micro-grid-operator-token
gcloud services enable secretmanager.googleapis.com --project="$GOOGLE_CLOUD_PROJECT"
gcloud secrets create "$API_TOKEN_SECRET" --replication-policy=automatic --project="$GOOGLE_CLOUD_PROJECT"
openssl rand -hex 32 | gcloud secrets versions add "$API_TOKEN_SECRET" --data-file=- --project="$GOOGLE_CLOUD_PROJECT"
bash deploy/cloud-run.sh
```

The script enables services, creates the Artifact Registry repository and Native
Firestore database if absent, grants the dedicated runtime identity Firestore and
Vertex access plus access to this token secret, builds both images with Cloud
Build, and deploys backend and dashboard Cloud Run services. The last line prints
the dashboard URL. Defaults: Firestore location `nam5`, Vertex location `global`,
Gemini model `gemini-2.5-flash`; override `FIRESTORE_LOCATION` or `GEMINI_MODEL`
before deployment if needed. Firestore database location cannot be changed later.
Retrieve the operator token through Secret Manager only in a trusted operator
session. The backend fails startup if its production token is missing.

**Security boundary:** Cloud Run reads and dashboard access are public in this
prototype. Mutations are protected by the application token over HTTPS, not Cloud
Run IAM. Do not store real household data here. For private deployments, remove
public access and introduce an authenticated gateway/IAP with service-to-service
identity; nginx currently does not mint Cloud Run identity tokens. For a public
event, add per-user authentication, rate limiting, and budget/quota controls
before sharing operator access. The token is shared operator authorization,
not a complete multi-user security system.

## Google Cloud services used

The local application runs without a Google Cloud account. The Cloud Run deployment
uses the core services below; the optional integrations are activated only when
their environment variables are configured. Cloud services may incur charges;
review current pricing and quotas before enabling them.

| Service | Project use | Configuration |
| --- | --- | --- |
| **Vertex AI / Gemini** | Gemini produces structured household bids in the deployed backend. The Gemini Developer API can also be used locally with an API key. | `GOOGLE_GENAI_USE_VERTEXAI`, `GOOGLE_CLOUD_PROJECT`, `GOOGLE_CLOUD_LOCATION`, `GEMINI_MODEL` |
| **Cloud Firestore** | Shared durable simulation state and negotiation logs, with revision-checked transactional writes. | `BACKEND_STORE=firestore` |
| **Cloud Run** | Runs the FastAPI backend and React dashboard as separate services. | `deploy/cloud-run.sh` |
| **Cloud Build** | Builds the backend and frontend container images. | `deploy/cloudbuild.yaml` |
| **Artifact Registry** | Stores the images deployed to Cloud Run. | Created by `deploy/cloud-run.sh` |
| **Secret Manager** | Supplies the backend operator token to Cloud Run without embedding it in the image. | `API_TOKEN_SECRET` |
| **Cloud Logging** | Receives Cloud Build logs (`CLOUD_LOGGING_ONLY`). | `deploy/cloudbuild.yaml` |
| **BigQuery (optional)** | Receives one JSON snapshot row after each successfully committed simulation revision. Create the target table before enabling it. | `BIGQUERY_TABLE=project.dataset.table` |
| **Cloud Storage (optional)** | Archives committed snapshots as JSON and provides the source bucket for image analysis. | `GCS_BUCKET` |
| **Cloud Vision API (optional)** | Returns image labels for a `gs://` image in the configured bucket. | `GCS_BUCKET`; endpoint requires the API token when configured |
| **Earth Engine (optional)** | Queries the `ECMWF/ERA5/HOURLY` collection for seven-day average solar irradiance context. This informational endpoint does not modify simulated dispatch. | `EARTH_ENGINE_PROJECT` |
| **Pub/Sub (optional)** | Publishes an event for each successfully committed revision; this project does not include a subscriber. | `PUBSUB_TOPIC` |
| **IAM / service accounts** | Gives the Cloud Run backend its runtime identity and access to enabled services and the operator secret. | Configured by the deployment script and service-specific IAM grants |

The optional integrations are independent and best-effort: a failed analytics,
archive, or Pub/Sub write does not roll back a committed simulation update. Image
analysis and Earth Engine requests instead return HTTP 503 when their service is
unavailable. `deploy/cloud-run.sh` enables the corresponding APIs when an optional
setting is present, but does not create buckets, BigQuery datasets/tables, or
Pub/Sub topics. Provision those resources and grant the runtime service account
least-privilege access before setting the options:

- BigQuery: grant `roles/bigquery.dataEditor` on the target dataset and create a
  table with `revision INT64`, `event STRING`, `updated_at TIMESTAMP`,
  `generation_id STRING`, and `snapshot_json STRING` columns.
- Cloud Storage: grant `roles/storage.objectCreator` on the archive bucket; grant
  `roles/storage.objectViewer` if images in that bucket will be analyzed.
- Pub/Sub: grant `roles/pubsub.publisher` on the topic.
- Earth Engine: register/enable the configured project with Earth Engine and grant
  the Cloud Run runtime identity Earth Engine access. Access and dataset
  availability are subject to Earth Engine eligibility and terms.

Only configure a service after its API, resource, permissions, and (for Earth
Engine) project access are ready. Example optional settings:

```bash
export BIGQUERY_TABLE="$GOOGLE_CLOUD_PROJECT.microgrid.dispatches"
export GCS_BUCKET="your-existing-microgrid-bucket"
export PUBSUB_TOPIC="micro-grid-dispatches"
export EARTH_ENGINE_PROJECT="$GOOGLE_CLOUD_PROJECT"
bash deploy/cloud-run.sh
```

## Validation

```bash
source .venv/bin/activate
python -m pytest backend/tests -q
cd frontend
npm run lint
npm run build
npm audit
```

Backend tests cover API validation/authentication, agent JSON/fallback behavior,
concurrency, and finite-interval energy/battery accounting. Cloud integration
requires credentials or an emulator; unit tests do not require either.

## Architecture and simulation boundaries

```text
React dashboard → FastAPI → concurrent household agents → Gemini structured bids
                         → community coordinator → atomic state/ledger store
                                                   (memory or Firestore)
```

- Storm reduces solar generation to 25% while utility supply remains connected.
  Outage islands the micro-grid with normal solar generation.
- Households propose load tiers (`full`, `essential`, `critical`), battery reserve,
  and energy exports. Essential and critical tiers retain 60% and 30% of full load.
- The coordinator enforces available energy, reserves, and battery power/capacity
  limits regardless of AI suggestions. It shares solar, serves local deficits
  from batteries, shares batteries while islanded, and reduces flexible demand
  when needed. Utility imports are allowed only when connected.
- Negotiation is a bounded bid/settlement round, not an unbounded LLM conversation.
  The coordinator is deliberately deterministic so AI cannot invent energy.
  Trades are physical allocations, not financial settlements or real payments.
- Batteries and wires are ideal/lossless; utility imports are unlimited while
  connected. This is an interactive software prototype, **not** a validated
  electrical network solver, real-grid controller, or safety-critical system.
- The node map highlights participating households through the community hub;
  detailed seller/buyer allocations are available in the ledger JSON.

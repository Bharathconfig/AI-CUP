# Micro-Grid Community Energy Trading & Resiliency Agent

A runnable full-stack hackathon prototype: four autonomous household agents submit
structured energy bids, and a community coordinator shares solar/battery energy
and sheds flexible loads during disruptions. A React dashboard displays the
neighborhood network, battery/solar metrics, and inspectable JSON negotiation logs.
FastAPI serves the simulation; Google Gemini supplies optional agent decisions.

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

When configured, mutations require `X-API-Key` matching `BACKEND_API_TOKEN`.
Enter the token in the dashboard operator field; it stays in memory only.
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
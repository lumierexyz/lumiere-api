# Lumiere backend

Financial-intelligence API for Lumiere: expense analytics, the **Personal Expense Index (PEI)**,
scenario forecasts of living costs, **Future Expense Coverage**, a grounded AI agent, and a
simulation-only hedging research engine.

Lumiere does not hold funds, issue tokens, run markets or execute trades. Forecasts, coverage and
portfolio results are conditional estimates and simulations.

## Architecture

```
app/
  core/           settings (env only), JSON logging without financial content, error envelope, rate limiting
  auth/           Supabase JWT verification (JWKS ES256/RS256, legacy HS256), CurrentUser/CurrentProfile deps
  database/       engine/session, portable column types, results tables (forecasts, simulations, agent)
  users/          profiles, audit log, profile update, data/account deletion
  transactions/   CSV normalisation, deterministic categorisation, duplicate detection, corrections
  expenses/       ledger with dated FX conversion, analytics (categories, recurring, volatility, trend...)
  economic_data/  FRED + Eurostat clients (retries, timeouts), DB cache, category->index mapping, FX rates
  forecasting/    price panel, PEI (chain-linked Laspeyres), scenario forecasting engine
  risk/           Future Expense Coverage engine
  ai/             OpenAI client, structured classification, agent + server-bound tools
  portfolio/      CVXPY hedging research over hypothetical instruments (simulation only)
  services/       audit, authorization-bound computation context shared by routes and agent tools
  api/            router registration (all endpoints under /api/v1)
```

Key rules the code enforces:

* **Identity** comes only from the verified token `sub`. No route accepts a `user_id`; every query filters
  by it, and objects owned by another user return 404. Postgres RLS policies (migration `0001`) add a
  second layer for any direct Supabase client access.
* **Calculations are deterministic Python** (pandas/numpy/CVXPY). The LLM only chooses which server tool
  to run and with which scenario parameters, receives aggregates (never descriptions or merchant names),
  and the response `sources` are produced by the server from the tools that actually ran.
* **No invented data**: missing FRED values (`.`) are skipped, failed series are marked `error`/`stale`,
  unsupported geographies and unconverted currencies are reported explicitly.
* **Currencies are never mixed** without an explicit FRED H.10 reference rate dated on or just before the
  transaction; otherwise rows are reported in `unconverted`.

### Financial methodology (summary; full text is returned by the API)

* **PEI**: monthly, chain-linked Laspeyres with annual re-weighting (year Y uses spending shares of
  Y-1, December link). Category price indices: BLS CPI-U via FRED (US) or Eurostat HICP COICOP 2018
  `prc_hicp_minr` (EU/EEA + CH). Missing category prices drop out with renormalisation; months with
  < 80 % of the basket priced are not published. First-year weights are contemporaneous (flagged).
* **Forecast**: per category, `base_c * (1 + pi_c + shift)^(m/12) * (1 + shock_c)`, base restated at the
  latest price level, `pi_c` = trailing 12-month inflation of the category index. 80 % bands from the
  dispersion of historical monthly totals (>= 6 months), assuming independent months.
* **Coverage**: reserves are drawn down against projected monthly spending, with a fractional final
  month; days = months x 30.436875. Nominal coverage (flat prices) is returned alongside.
* **Portfolio research**: scenarios ~ multivariate normal fitted to historical 12-month category
  inflation; hypothetical instruments (cash, inflation-linked note, category trackers, binary CPI
  contract); minimise mean squared shortfall vs PEI inflation subject to budget, long-only and
  per-instrument caps. Reports tracking error, hedge effectiveness vs cash, CVaR95 and sensitivities.

## Run locally

```bash
uv sync                      # Python 3.12
cp .env.example .env         # fill SUPABASE_URL at minimum; DATABASE_URL may stay sqlite for a quick start
uv run alembic upgrade head
uv run uvicorn app.main:app --reload --port 8000
# OpenAPI UI: http://localhost:8000/docs   (disabled when ENVIRONMENT=production)
```

Quality gates:

```bash
uv run ruff check . && uv run ruff format --check . && uv run pytest
```

## Supabase setup

1. Create the project; in **Authentication** enable email (or your providers). New projects sign access
   tokens with asymmetric keys, verified through `SUPABASE_URL/auth/v1/.well-known/jwks.json`. For a
   legacy HS256 project set `SUPABASE_JWT_SECRET`.
2. Copy the **Session pooler** connection string into `DATABASE_URL` as
   `postgresql+psycopg://...?...sslmode=require`.
3. Run `uv run alembic upgrade head` (or paste `docs/schema-postgres.sql` into the SQL editor). This
   creates the tables, links `profiles.id` to `auth.users(id) ON DELETE CASCADE`, enables RLS with
   owner-only policies on user tables, read-only policies on reference data, and no client access to
   `audit_logs`.
4. Keep `SUPABASE_SERVICE_ROLE_KEY` server-side only (Cloud Run secret). It is optional and used solely
   to delete the auth user on `DELETE /api/v1/me?confirm=true`.

## Deploy to Google Cloud Run

```bash
PROJECT=your-gcp-project REGION=europe-west1
gcloud artifacts repositories create lumiere --repository-format=docker --location=$REGION
gcloud builds submit --tag $REGION-docker.pkg.dev/$PROJECT/lumiere/api:latest

# Secrets (once)
for s in DATABASE_URL SUPABASE_SERVICE_ROLE_KEY FRED_API_KEY OPENAI_API_KEY; do
  printf '%s' "${!s}" | gcloud secrets create $s --data-file=-; done

# Migrations as a Cloud Run job, then the service
gcloud run jobs create lumiere-migrate --image $REGION-docker.pkg.dev/$PROJECT/lumiere/api:latest \
  --region $REGION --command alembic --args upgrade,head --set-secrets DATABASE_URL=DATABASE_URL:latest
gcloud run jobs execute lumiere-migrate --region $REGION --wait

gcloud run deploy lumiere-api --image $REGION-docker.pkg.dev/$PROJECT/lumiere/api:latest --region $REGION \
  --allow-unauthenticated --port 8080 --min-instances 0 --max-instances 10 \
  --set-env-vars ENVIRONMENT=production,SUPABASE_URL=https://PROJECT_REF.supabase.co,CORS_ORIGINS=https://your-frontend \
  --set-secrets DATABASE_URL=DATABASE_URL:latest,SUPABASE_SERVICE_ROLE_KEY=SUPABASE_SERVICE_ROLE_KEY:latest,FRED_API_KEY=FRED_API_KEY:latest,OPENAI_API_KEY=OPENAI_API_KEY:latest
```

`--allow-unauthenticated` exposes the service to the internet; every `/api/v1` route still requires a valid
Supabase session. Use `/health` for liveness and `/ready` for readiness (database reachable, auth configured).

## Frontend integration

See [`docs/API.md`](docs/API.md) for the endpoint contract, example requests and a typed fetch helper,
and `docs/openapi.json` for the full schema (generate TypeScript types with `openapi-typescript`).

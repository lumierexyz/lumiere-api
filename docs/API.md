# Lumiere API contract (v1)

Base URL: `https://<cloud-run-host>/api/v1`. Every call except `/health` and `/ready` needs
`Authorization: Bearer <supabase access_token>`.

Errors always look like:

```json
{ "error": { "code": "insufficient_data", "message": "No expense history yet. Import a bank statement first.", "details": null } }
```

Common codes: `unauthorized` (401), `not_found` (404, also for other users' objects), `validation_error` (422),
`insufficient_data` / `unsupported_geography` (422), `conversion_unavailable` (400), `payload_too_large` (413),
`rate_limited` (429, with `Retry-After`), `upstream_unavailable` / `provider_not_configured` (502/503),
`ai_unavailable` (503).

| Method | Path | Purpose |
|---|---|---|
| GET | `/health`, `/ready` | Liveness / readiness (no auth) |
| GET, PATCH | `/me` | Profile: `display_name`, `base_currency`, `country` (ISO-2), `date_order` |
| DELETE | `/me/data` | Erase all financial data, keep the account |
| DELETE | `/me?confirm=true` | Delete account and all data |
| POST | `/transactions/import` | Multipart CSV upload (`file`, optional `currency`, `date_order`, `sign_convention`) |
| GET | `/transactions` | Paginated (`limit`, `offset`), filters `start`, `end`, `category`, `kind` |
| PATCH | `/transactions/{id}` | Correct `category` and/or `kind` (marked `user_corrected`, never overwritten) |
| POST | `/transactions/categorize` | LLM classification of rows the rules could not place |
| GET | `/imports` | Import history with counts |
| GET | `/expenses/summary` | Dashboard metrics (optional `start`, `end`) |
| GET | `/expenses/categories` | Category totals, shares, concentration |
| GET | `/expenses/history` | Monthly totals by category, newest first (paginated) |
| GET | `/expenses/recurring` | Detected recurring charges |
| GET | `/economic-data/series` | Category→index mapping and freshness for the user's country (`refresh=true` forces) |
| POST | `/economic-data/fx/refresh` | Load FRED H.10 rates for the user's currencies |
| GET | `/pei`, `/pei/history`, `/pei/composition` | Personal Expense Index |
| POST, GET | `/forecasts`, `/forecasts/{id}` | Scenario projections (3/6/12/24 months) |
| POST | `/coverage/calculate` | Future Expense Coverage |
| POST | `/agent/chat` | Grounded assistant; `conversation_id` to continue |
| GET, DELETE | `/agent/conversations[/{id}]` | Conversation history |
| POST, GET | `/portfolio/simulate`, `/portfolio/simulations/{id}` | Simulation-only hedging research |

## Examples

```bash
TOKEN=...   # supabase.auth.getSession() -> session.access_token
API=https://<host>/api/v1

curl -X PATCH $API/me -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"country":"FR","base_currency":"EUR","date_order":"DMY"}'

curl -X POST $API/transactions/import -H "Authorization: Bearer $TOKEN" \
  -F file=@releve.csv -F currency=EUR
# -> {"import_id":"...","rows":412,"imported":409,"duplicates":0,"rejected":3,"rejected_rows":[{"line":57,"reason":"invalid_amount"}],
#     "warnings":[],"normalization":{"delimiter":";","date_format":"%d/%m/%Y","decimal_separator":",",...}}

curl $API/pei -H "Authorization: Bearer $TOKEN"
# -> {"value":103.82,"as_of_month":"2026-09","base_month":"2025-01","year_over_year":0.0282,
#     "headline_year_over_year":0.0342,"gap_vs_headline_yoy_pp":-0.6,"basket_coverage":1.0,"flags":[...],
#     "methodology":{...},"sources":[{"category":"housing","source":"eurostat","series_id":"CP041",...}],"disclaimer":"..."}

curl -X POST $API/forecasts -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"horizon_months":12,"scenarios":[{"name":"baseline"},{"name":"energy +20%","shocks":{"energy":0.2}}]}'

curl -X POST $API/coverage/calculate -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"reserves":20000,"currency":"EUR"}'
# -> {"nominal":{"months":9.66,...},"projected":[{"scenario":"baseline","months":9.54,"days":290.4,...},...],...}

curl -X POST $API/agent/chat -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"message":"What happens if energy prices increase by 20%?"}'
# -> {"conversation_id":"...","answer":"...","sources":[{"type":"calculation","tool":"forecast_expenses"},
#     {"type":"economic_series","source":"eurostat","series_id":"NRG",...}],"assumptions":[...],"grounded":true}
```

## Typed fetch helper (Next.js / TypeScript)

```ts
import { createClient } from "@supabase/supabase-js";

const supabase = createClient(process.env.NEXT_PUBLIC_SUPABASE_URL!, process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY!);
const API = process.env.NEXT_PUBLIC_LUMIERE_API!; // e.g. https://lumiere-api-xxxx.run.app/api/v1

export class ApiError extends Error {
  constructor(public status: number, public code: string, message: string, public details?: unknown) { super(message); }
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const { data } = await supabase.auth.getSession();
  const token = data.session?.access_token;
  if (!token) throw new ApiError(401, "unauthorized", "Not signed in");
  const res = await fetch(`${API}${path}`, {
    ...init,
    headers: { ...(init.body instanceof FormData ? {} : { "Content-Type": "application/json" }),
               Authorization: `Bearer ${token}`, ...init.headers },
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new ApiError(res.status, body?.error?.code ?? "http_error", body?.error?.message ?? res.statusText, body?.error?.details);
  }
  return res.status === 204 ? (undefined as T) : res.json();
}

// usage
const pei = await api<{ value: number; year_over_year: number | null }>("/pei");
const form = new FormData(); form.append("file", file); form.append("currency", "EUR");
await api("/transactions/import", { method: "POST", body: form });
```

Generate full response types from the schema: `npx openapi-typescript docs/openapi.json -o lumiere-api.d.ts`.

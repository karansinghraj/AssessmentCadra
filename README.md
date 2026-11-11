# Larkfield Home — Support Email Triage Agent

An HTTP service that reads one customer-support email and returns the desk's decision. It
**proposes only**: it never refunds, sends email, or changes customer data.

- Design: [`ARCHITECTURE.md`](ARCHITECTURE.md)
- Code structure and logic: [`CODE.md`](CODE.md)
- Vercel serverless: [`VERCEL.md`](VERCEL.md)
- Build plan: [`PLAN.md`](PLAN.md)
- Current state / handoff: [`MEMORY.md`](MEMORY.md)
- Approach: [`APPROACH.md`](APPROACH.md)
- Artefact: [`ARTEFACT.md`](ARTEFACT.md)

---

## Quick start

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt

# 1. Prepare data (one command, reproducible, never edits data/)
python -m src.prepare_data --report

# 2. Run the service
uvicorn src.app:app --host 0.0.0.0 --port 8000
```

`GET http://localhost:8000/health` → `{"status":"ok"}`.

### Model configuration

The service works without a model key (deterministic rule-based path). To enable the optional
classifier and final judge, set your own OpenAI-compatible provider:

```bash
export LLM_API_KEY=...               # never commit this
export LLM_BASE_URL=https://api.openai.com/v1
export LLM_MODEL=gpt-4o-mini
export LLM_INPUT_USD_PER_MTOK=0.15
export LLM_OUTPUT_USD_PER_MTOK=0.60
```

The deployed service uses its own key. The coding agent's build-time token is never shipped.

---

## API

| Method | Path | Body | Response |
|---|---|---|---|
| GET | `/health` | — | `200 {"status":"ok"}` |
| POST | `/tickets` | `Ticket` | full run (contract fields + added fields) |
| GET | `/runs/{run_id}` | — | current run, including `AWAITING_APPROVAL` |
| POST | `/runs/{run_id}/decision` | `{"decision":"approve"\|"reject","approver":"string"}` | updated run |
| GET | `/ledger` | — | recorded action rows |

Interactive docs: **`/docs`** (Swagger UI), **`/redoc`** (ReDoc) and **`/openapi.json`**. Every
route has a response model, summary, description and documented error responses.

### Logging

Every API request and lifecycle event is one JSON line: `request_id`, method, path, status,
latency, ticket/run ids, outcome, state, amount, classification path, tokens and cost. Email bodies
and addresses are never logged. `LOG_LEVEL` controls verbosity (default `INFO`); uvicorn's duplicate
access line is suppressed because the middleware owns access logging.

### Example

```bash
curl -s -X POST http://localhost:8000/tickets \
  -H "Content-Type: application/json" \
  --data @public_ticket.json

curl -s http://localhost:8000/runs/<run_id>

curl -s -X POST http://localhost:8000/runs/<run_id>/decision \
  -H "Content-Type: application/json" \
  -d '{"decision":"approve","approver":"support-lead@larkfield"}'

curl -s http://localhost:8000/ledger
```

---

## Added fields (contract allows extra fields)

Run-level:

| Field | Meaning |
|---|---|
| `action` | `{action, amount_usd, line_no, item, status}` — the proposed/recorded action |
| `state` | `AWAITING_APPROVAL`, `APPROVED`, `REJECTED`, `ESCALATED`, `CLOSED` |
| `decisions` | `[{decision, approver, decided_at}]` — audit history |
| `decision` | `null` until decided, then the same object as the first `decisions` entry |

Trace-level:

| Field | Meaning |
|---|---|
| `latency_ms` | measured end-to-end policy + optional model time |
| `tokens_total` | input + output tokens across calls |
| `pricing` | model and per-million-token prices used for `cost_usd` |
| `classification_path` | `pattern`, `pattern_guardrail`, `pattern_fallback`, or `llm` |
| `classification_route` | router decision: `pattern`, `guardrail`, or `llm` |
| `classifier_used` | whether the optional classifier was called |
| `judge` | final LLM judge verdict/issues (null if not configured) |
| `sanity_issues` | deterministic final-sanity failures (empty on success) |
| `calls_detail` | per-call model, tokens, cost, latency, error |
| `processed_at` | UTC timestamp |

Ledger-level: added `approver` and `decided_at` (null until a decision).

**After a decision**, `outcome` remains `PEND` (the policy decision that was proposed); `state`
becomes `APPROVED`/`REJECTED`, `human_approval_required` becomes `false`, and the single ledger row
is updated in place. A repeated decision is idempotent; a conflicting decision returns `409`.

---

## Decision rules (one line each)

- **Policy precedence:** RP-3.2 v3.2 is authoritative; FAQ v2 is superseded; the warehouse doc does
  not set customer-care rules.
- **Window:** refund/return within 30 days of carrier delivery; 31–45 days only for damage/fault
  with an image attachment; otherwise `REJECT` / `OUT_OF_WINDOW`.
- **Amount:** affected `unit_price × quantity`, capped at ordered quantity, minus issued and locally
  approved refunds, never below zero; shipping is excluded.
- **Authority:** above **$150** → `PEND` + `SUPPORT_LEAD` + `APPROVAL_REQUIRED`.
- **Injection/chargeback/legal/pre-approval:** `ESCALATE` + `TRUST_SAFETY`, no refund.
- **Lost transit:** no carrier movement **> 10 days** → `ESCALATE` + `CARRIER_CLAIMS`; exactly 10
  days is not lost.
- **Unreadable carrier:** `UNABLE_TO_VERIFY` / `CARRIER_STATUS_UNAVAILABLE`, never a pass.
- **Invalid sender/order:** `INVALID_REQUEST` / `CUSTOMER_MISMATCH` or `ORDER_NOT_FOUND`; never act on
  a nearby order.
- **Partial request:** whole-order or shipping asks when only some lines are eligible →
  `PROCEED_MODIFIED` + `PARTIAL_LINES`.
- **Unknown/unresolvable:** generic support reply; no action.

---

## Data preparation

`python -m src.prepare_data` reads `data/*.csv` **without modifying them** and writes:

- `build/data.json` — normalised read model
- `build/manifest.json` — source SHA-256 hashes, counts, rules, data issues

It is deterministic (sorted keys, no timestamps) and rebuilds automatically at startup if the
sources change. It handles:

- order ids: `LF-100200`, `LF100201`, `#100202`, `order 100203`, `order order 100204`
- slash dates as `DD/MM/YYYY` (proved by unambiguous values in the export)
- money as `$129.00`, `USD 24.00`, `129.00`
- duplicate order rows: identical rows collapse, conflicting rows quarantine
- shipments: missing, `UNAVAILABLE`, and scans before shipment are flagged
- refunds: only `ISSUED` amounts are subtracted, per order line

Report from the current export: 53 customers, 49 order rows → 47 unique orders, 57 lines,
47 shipments, 3 issued refunds; 2 duplicate order rows; 13 slash dates; 3 `UNAVAILABLE` shipments;
4 scan-before-ship anomalies.

---

## Architecture in brief

```
ticket
  → pattern router (intent, order refs, damage/wrong, injection)   [no model needed]
  → optional LLM classifier only when the pattern layer is unsure
  → deterministic policy engine (checks, amount, window, authority)
  → deterministic sanity gate (fail closed)
  → optional LLM final judge (can only block, never rewrite money)
  → trace + SQLite run/ledger
```

The LLM never computes money and never writes the ledger. If the provider fails, the rule-based
path still returns a safe decision; the trace records `rule-based-fallback` with 0 tokens.

State is stored in SQLite (`RUN_DB_PATH`, default `var/runs.db`) with:

- one run per `ticket_id`
- one decision per run (first wins)
- one ledger row per `(run_id, action)`

---

## Tests and evaluation

```bash
python -m pytest -q                 # 15 passed
python -m scripts.dry_run           # 20/20 end-to-end checks

# 40 cases: 15 labelled + 10 public + 15 new negatives/boundaries
python -m eval.run_eval --offline --label after
python -m eval.run_eval --offline --baseline --label before

# with a provider key configured, drop --offline to measure real tokens/cost/latency
python -m eval.run_eval --label after
```

Pass rule: outcome, `human_approval_required`, `approver_role`, amount within $0.01, and the exact
reason-code set must all match.

Committed results:

- `eval/results/20261111T141802Z_before.json` — 37/40 (92.5%); missing 45-day photo exception
- `eval/results/20261111T141806Z_after.json` — 40/40 (100%)

Current offline run: median latency <1 ms, median cost $0.000000 (no model). With a key, the judge
and classifier token usage are read from the provider and converted with the configured prices.

---

## Deployment

### Vercel (serverless)

`main.py` is the Vercel entrypoint and `vercel.json` configures the function. When `VERCEL=1`,
SQLite and the prepared-data cache default to `/tmp`; nothing is written to the read-only repo.

```bash
npm i -g vercel
vercel login
vercel --prod
```

Set `LLM_API_KEY`, `LLM_BASE_URL`, `LLM_MODEL`, prices and `LOG_LEVEL` in Vercel. Leave
`RUN_DB_PATH` and `DATA_BUILD_DIR` unset. Full steps and caveats: [`VERCEL.md`](VERCEL.md).

> `/tmp` is per instance and ephemeral: runs, decisions and the ledger do not survive a cold start
> and are not shared between concurrent instances. Use Postgres for durable production state.

Validate the serverless path locally:

```bash
python -m scripts.vercel_smoke
```

### Render (Docker)

1. Push this repo to a public GitHub repository.
2. Create a Render Web Service from the repo; Render reads `render.yaml` (Docker runtime).
3. Set `LLM_API_KEY` (secret) and, if different, `LLM_BASE_URL` / `LLM_MODEL`.
4. Deploy; health check is `/health`. The start command reads `PORT`.

`RUN_DB_PATH=/tmp/larkfield-runs.db` on the free tier: runs persist for the life of the instance,
but the free instance may restart. Use a persistent disk or Postgres for production.

### Local

```bash
uvicorn src.app:app --host 0.0.0.0 --port ${PORT:-8000} --no-access-log
```

---

## Repository layout

```
src/
  app.py            FastAPI routes, OpenAPI metadata, route logs
  api_models.py     Swagger response models (contract + added fields)
  middleware.py     request id / timing / JSON access logs
  logging_config.py JSON formatter and logger setup
  pipeline.py       routing + policy + sanity + judge orchestration
  guardrails.py     pattern router + injection + sanity gate
  extract.py        optional LLM classifier and judge
  policy.py         deterministic policy engine
  replies.py        deterministic customer replies
  prepare_data.py   one-command data build
  normalize.py      normalisation helpers
  store.py          SQLite runs / decisions / ledger
scripts/
  dry_run.py        end-to-end flow validation (no model key)
  vercel_smoke.py   serverless /tmp path + entrypoint validation
eval/
  cases.json        40 cases and pass rule
  run_eval.py       runner with timestamped results
tests/              normaliser, policy, API, Swagger, logging, config, LLM trace
data/               source exports (never edited)
policies/           source policy documents (never edited)
inbox/ tickets/     source tickets (never edited)
schema/             contract models (never edited)
main.py             Vercel serverless entrypoint
vercel.json         Vercel function config
```

---

## Limitations

- No actual refund execution, email sending, or carrier API calls (by contract).
- Attachments are used as a boolean signal only; image content is not analysed (RP-3.2 §2.6 says a
  supplied image is sufficient for this desk).
- Approver identity is a free-text name; the contract provides no authentication.
- Multi-order or conflicting-intent emails return `UNABLE_TO_VERIFY` for human review rather than
  being split automatically.
- SQLite is single-instance; free-host ephemeral storage may lose runs on restart. On Vercel,
  `/tmp` is per instance and ephemeral, so runs/decisions/ledger do not survive cold starts or
  scale across instances. Use Postgres for durable production state.
- Email-match is the identity check; spoofing/account takeover is out of scope.

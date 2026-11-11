# CODE — structure and functional logic

This document explains how the service is put together and what each layer does at runtime. It is
the companion to `ARCHITECTURE.md` (design rationale) and `MEMORY.md` (handoff status). Last
validated: 2026-11-11 with `python -m scripts.dry_run` (20/20 checks), `python -m pytest -q` (15
passed) and the 40-case evaluation (40/40 after).

---

## 1. Runtime shape

```
HTTP client
   │
   ▼
FastAPI app (src/app.py)
   │  RequestContextMiddleware (src/middleware.py)
   │    - X-Request-ID / X-Response-Time-ms
   │    - JSON access log
   ▼
route ──► store (SQLite) ──► pipeline (src/pipeline.py)
                               │
                               ├─ guardrails.analyse()      pattern router / injection
                               ├─ extract.classify()        optional, low-confidence only
                               ├─ policy.decide()           deterministic money + outcome
                               ├─ guardrails.sanity_check() fail closed
                               ├─ extract.judge()           optional, block-only
                               └─ replies.compose()         deterministic reply text
```

The process builds prepared data and the SQLite store once in `create_app()`. `uvicorn
src.app:app` serves the resulting app.

---

## 2. Directory map

```
src/
  app.py             FastAPI routes, OpenAPI metadata, response models, route logs
  api_models.py      Swagger-only response models (contract + added fields)
  middleware.py      request id, timing, JSON access log, 500 wrapper
  logging_config.py  JSON formatter + logger configuration
  config.py          environment config, prices, cost calculation
  normalize.py       email / order / date / money / text normalisation
  prepare_data.py    one-command deterministic data build + manifest
  guardrails.py      pattern router, injection patterns, output sanity gate
  extract.py         optional LLM classifier, final judge, model-call trace
  policy.py          checks, eligibility, amount, authority, outcome table
  replies.py         deterministic customer-facing reply templates
  store.py           SQLite runs / decisions / ledger, idempotency
  pipeline.py        orchestration and trace assembly
scripts/
  dry_run.py         end-to-end TestClient flow validation (no key needed)
eval/
  cases.json         40 expected cases + pass rule
  run_eval.py        runner, contract checks, timestamped results
tests/
  test_api.py        endpoints, idempotency, Swagger, logging
  test_normalize_policy.py  normalisation and policy matrix
  test_llm_trace.py  fake-provider classifier + judge trace
schema/
  response_schema.py supplied contract request/response models (not edited)
```

---

## 3. Request lifecycle

### 3.1 `POST /tickets`

1. FastAPI validates the body against `schema.response_schema.Ticket`.
2. `create_ticket()` checks `store.get_run_by_ticket(ticket_id)`.
   - Existing run: return it unchanged and log `ticket_reused`. No new ledger row.
3. `pipeline.process_ticket()` builds the response (section 4).
4. `store.create_run()` inserts the run **and** one ledger row in the same transaction.
5. The response is validated/serialised by `RunResponse` and returned.

The route logs `ticket_processed` with ids, outcome, state, approver role, amount, classification
path, latency, tokens and cost. It never logs the email body.

### 3.2 `GET /runs/{run_id}`

Reads `runs.response_json`, overlays the current `state`, attaches `decisions`, and returns the run.
Unknown id → `404 run_not_found`.

### 3.3 `POST /runs/{run_id}/decision`

1. `approver.strip()` must be non-empty → otherwise `422 approver_required`.
2. Unknown run → `404`.
3. If already `APPROVED`/`REJECTED`:
   - same decision → return the run (idempotent);
   - different decision → `409 run_already_decided`.
4. If not `AWAITING_APPROVAL` → `409 run_not_waiting_for_approval`.
5. Insert the first decision (`store.add_decision`, unique on `run_id`).
6. Set `state` to `APPROVED`/`REJECTED`, clear the approval flags, store `decision` and
   `decisions`, update the single ledger row in place.
7. Return the updated run.

`outcome` is deliberately unchanged: it records what the policy proposed. `state` records the
human lifecycle.

### 3.4 `GET /ledger`

Returns all ledger rows ordered by `recorded_at, run_id`, with `amount_usd` rounded to cents.
One row exists per `(run_id, action)`; approval updates status/approver/decided time.

### 3.5 `GET /health` and docs

- `/health` returns exactly `{"status":"ok"}`.
- `/docs` is Swagger UI, `/redoc` is ReDoc, `/openapi.json` is the schema.
- All five routes have response models, summaries, descriptions and error responses.

---

## 4. Pipeline logic (`src/pipeline.py`)

```
process_ticket(ticket, data, store, cfg)
  started = perf_counter()
  analysis = guardrails.analyse(ticket)
  if cfg.llm_configured and cfg.llm_classifier_enabled and analysis.route == "llm":
      classifier = extract.classify(ticket, analysis, cfg)
      analysis = merge(pattern, classifier.data)
  local_refunds = store.approved_refunds_for_order(order_id)   # only if one order ref
  decision = policy.decide(ticket, data, analysis, local_refunds)
  response = contract fields + action + state + decisions
  sanity_issues = guardrails.sanity_check(response)
  if sanity_issues: response = fail_closed(response, sanity_issues)
  if cfg.llm_configured and cfg.llm_judge_enabled:
      judged = extract.judge(ticket, response, analysis, cfg)
      if judged.hard_safety_issue: response = fail_closed(response, issues, "llm_judge")
  attach trace (model calls, tools, documents, cost, latency, route, judge, sanity)
```

Key details:

- **Classifier gating.** The model is not called just because a key exists. It is called only when
  the pattern router returns `route == "llm"` (low confidence).
- **Merge rule.** Pattern facts win; the classifier may fill missing order refs/product and may set
  a type when the pattern type is `OTHER`; boolean flags are OR-ed. Unknown enum values are dropped.
- **No money from the model.** `policy.decide()` owns all amounts, eligibility, authority and
  outcomes.
- **Judge is block-only.** A hard-safety verdict calls `_fail_closed()`, which forces
  `UNABLE_TO_VERIFY`, removes any refund amount, attaches an `OUTPUT_SANITY` check and a generic
  support reply. The judge cannot change an amount or turn a reject into a proceed.
- **Fallback trace.** If no model call happens, the trace contains one synthetic call with
  `model="rule-based-fallback"`, zero tokens/cost and `error="llm_not_configured_or_not_needed"`.

Trace fields added per run:

| Field | Meaning |
|---|---|
| `model_calls[]` | contract shape: model, input/output tokens |
| `tools_called[]` | `pattern_router`, `llm_classify`, `llm_judge` as used |
| `documents_used[]` | policy docs and versions consulted |
| `cost_usd` | sum of per-call cost |
| `latency_ms` | end-to-end pipeline time |
| `tokens_total` | input + output tokens |
| `pricing` | model and per-million prices used |
| `classification_path` | `pattern`, `pattern_guardrail`, `pattern_fallback`, `llm` |
| `classification_route` | router decision: `pattern`, `guardrail`, `llm` |
| `classifier_used` | whether the classifier ran |
| `judge` | verdict, hard-safety flag, issues, latency, ok |
| `sanity_issues` | deterministic gate failures |
| `calls_detail[]` | per-call model, tokens, cost, latency, ok, error |
| `processed_at` | UTC timestamp |

---

## 5. Pattern router and sanity gate (`src/guardrails.py`)

### 5.1 `analyse(ticket)`

Normalises subject + body to lowercase and applies regex groups:

| Group | Examples | Effect |
|---|---|---|
| injection | "ignore previous instructions", "pre-approved", "chargeback", "legal action", "bypass policy" | `injection=True`, route `guardrail` |
| damage | damaged, broken, cracked, faulty, defective, not working | damage signal |
| wrong item | wrong item, received something else, not as described | wrong-item signal |
| non-delivery | never arrived, not received, lost in transit, missing | delivery dispute |
| stuck | stuck, no movement, no tracking update | transit issue |
| status | where is my order, tracking, status | order-status intent |
| return / refund | return, send it back / refund, money back, reimburse | return/refund intent |
| whole order / shipping | whole order, entire order / shipping charge, postage | partial-line detection |
| prior refund reference | already refunded, refund has come, again | duplicate-credit hint |

It also extracts canonical order refs (`extract_order_refs`) and a product hint from phrases such as
"refund the broken lamp".

Intent order:

```
refund/damage/wrong-item → REFUND
else return              → RETURN
else delivery issue      → DELIVERY_ISSUE
else status request      → ORDER_STATUS
else                     → OTHER
```

Route and confidence:

```
injection                     → route=guardrail, confidence=0.98
confidence >= 0.60            → route=pattern
otherwise                     → route=llm
```

Base confidence is 0.92; subtract 0.40 with no order ref, 0.45 with multiple refs, 0.45 for
`OTHER`, 0.20 when no issue signal is present; clamp to 0.05–0.99.

### 5.2 `sanity_check(response)`

Fail-closed invariants:

- PEND/ESCALATE must have `human_approval_required=true`.
- Other outcomes must not require approval.
- PEND must target `SUPPORT_LEAD`; ESCALATE must target `CARRIER_CLAIMS` or `TRUST_SAFETY`.
- Amount > $150 may not be PROCEED / PROCEED_MODIFIED / INVALID_REQUEST.
- INVALID_REQUEST may not carry a positive amount.
- A positive amount requires `action.action == "REFUND"`.
- The reply body must contain the formatted amount and the resolved order id.
- No PROCEED with a positive amount if a check is `UNABLE_TO_VERIFY` or `FAIL`.
- No `PASS` on a check marked `incomplete`.

Any issue triggers `pipeline._fail_closed()`.

---

## 6. Policy engine (`src/policy.py`)

`decide(ticket, data, analysis, local_refunds, baseline_without_damage_exception)` is the only
place that decides eligibility and money.

### 6.1 Order of decisions

1. **Base checks:** `POLICY_PRECEDENCE`, `CUSTOMER_IDENTITY`.
2. **Injection:** add `TICKET_INJECTION`; if true → `ESCALATE` / `TRUST_SAFETY` /
   `POLICY_OVERRIDE_ATTEMPT`, action `ESCALATION`, amount `null`.
3. **OTHER intent:** generic reply, `PROCEED`, action `REPLY`, no amount.
4. **Unknown sender:** `INVALID_REQUEST` / `CUSTOMER_MISMATCH`.
5. **No order ref:** `INVALID_REQUEST` / `ORDER_NOT_FOUND`.
6. **Multiple order refs:** `UNABLE_TO_VERIFY` with `ORDER_EXISTS=UNABLE_TO_VERIFY`.
7. **Order not found:** `INVALID_REQUEST` / `ORDER_NOT_FOUND`.
8. **Ownership mismatch:** `INVALID_REQUEST` / `CUSTOMER_MISMATCH`.
9. **Quarantined duplicate order:** `UNABLE_TO_VERIFY`.
10. **ORDER_STATUS / DELIVERY_ISSUE:** carrier checks; delivered → status reply; no movement >
    10 days → `ESCALATE` / `CARRIER_CLAIMS` / `LOST_IN_TRANSIT`; otherwise status reply.
11. **Refund / return:** carrier availability, delivery scan, window, matching line, quantity,
    prior refunds, amount, authority, partial-line modification.

### 6.2 Refund amount

```
ordered_qty = line.qty
affected_qty = 1 by default
             = ordered_qty for "both"
             = min(spoken quantity, ordered_qty)
             = min(1, ordered_qty) for "one of ..."
line_total   = unit_price_cents × ordered_qty
prior_total  = ISSUED refunds for the line + locally approved proposals
unpaid       = max(0, line_total − prior_total)
amount       = min(unit_price_cents × affected_qty, unpaid)
```

- Never negative; never above the unpaid line value.
- Shipping is excluded.
- If `amount == 0` → `REJECT` / `ALREADY_REFUNDED` (the reply explains the earlier credit).
- `> 15000` cents → `PEND` + `SUPPORT_LEAD` + `APPROVAL_REQUIRED`.

### 6.3 Window and exceptions

```
days = received_date − delivered_date
days <= 30                                  → PASS
31 <= days <= 45 and damage and image        → PASS + DAMAGE_EXCEPTION
otherwise                                    → FAIL + OUT_OF_WINDOW
```

The 31–45 path can be disabled with `DISABLE_DAMAGE_EXCEPTION=1`, which is how the baseline eval
run is produced.

### 6.4 Partial-line requests

`PROCEED_MODIFIED` + `PARTIAL_LINES` when:

- the customer asks for the whole order but only one line is eligible, or
- the customer asks for the shipping charge, which is excluded from item value.

### 6.5 Check ids emitted

`POLICY_PRECEDENCE`, `CUSTOMER_IDENTITY`, `TICKET_INJECTION`, `ORDER_EXISTS`,
`ORDER_OWNERSHIP`, `CARRIER_STATUS`, `DELIVERY_SCAN`, `CARRIER_MOVEMENT`, `RETURN_WINDOW`,
`DAMAGE_PHOTO_EXCEPTION`, `ITEM_IN_ORDER`, `AFFECTED_QUANTITY`, `PRIOR_REFUND`,
`REFUND_AMOUNT`, `AUTHORITY_LIMIT`, plus `OUTPUT_SANITY` when the sanity gate or judge blocks.

Every check carries `evidence[]` with source, ref, value and a `policy_clause`.

### 6.6 Reply composition (`src/replies.py`)

Templates are selected by outcome and reason codes. The reply receives amount, order id, item,
dates, carrier and prior-credit facts from the decision object, so the text cannot disagree with the
action. `generic_fallback()` is the safe support reply used when the pipeline fails closed.

---

## 7. LLM layer (`src/extract.py`)

- `_chat()` calls an OpenAI-compatible `/chat/completions` endpoint with `temperature=0`, an 8 s
  timeout and no retries inside the function. Provider usage becomes `ModelCall`.
- `classify()` asks for strict JSON extraction only. It validates enum values, normalises order
  refs, coerces booleans and drops unknown fields.
- `judge()` receives the ticket and a compact decision summary and returns
  `{verdict, hard_safety_issue, issues}`. A hard-safety issue can only fail the run closed.
- Failures (no key, timeout, bad JSON) are non-fatal: `ok=False`, zero cost, and the deterministic
  path proceeds.

The model is never asked to compute an amount, choose an approver or write the ledger.

---

## 8. Data build (`src/prepare_data.py`)

`python -m src.prepare_data` reads `data/*.csv` without modifying them and writes
`build/data.json` plus `build/manifest.json`.

- Order ids normalised to `LF-######`.
- Slash dates inferred as `DD/MM/YYYY` from unambiguous values.
- Money converted to integer cents.
- Identical duplicate order rows collapse; conflicting rows are quarantined.
- Shipments missing/`UNAVAILABLE` and scans before `shipped_at` are flagged.
- Only `ISSUED` refunds are summed, per `(order_id, line_no)`.
- `ensure_built()` rebuilds automatically if source hashes change; hashes and counts live in the
  manifest.

Prepared counts: 53 customers, 49 order rows → 47 orders, 57 lines, 47 shipments, 3 issued refunds.

---

## 9. Storage and idempotency (`src/store.py`)

SQLite tables:

| Table | Key / constraint | Purpose |
|---|---|---|
| `runs` | `run_id` PK, `ticket_id` UNIQUE | ticket snapshot, response JSON, state, outcome |
| `decisions` | `UNIQUE(run_id)` | first human decision wins |
| `ledger` | `PRIMARY KEY(run_id, action)` | one action row per run/action |

Important methods:

- `create_run()` — returns the existing response if `ticket_id` already exists; otherwise inserts
  the run and the first ledger row atomically.
- `get_run()` / `get_run_by_ticket()` — hydrate response JSON, current state and decisions.
- `add_decision()` — inserts once; returns `(inserted, existing)`.
- `update_ledger()` — updates the ledger row in place on decision.
- `approved_refunds_for_order()` — sums approved local proposals so a forwarded duplicate cannot be
  paid twice.

The store uses a process-wide `RLock` and WAL mode. It is single-instance by design; production
would move to Postgres.

---

## 10. API layer: logging and Swagger

### 10.1 Logger

`src/logging_config.py` installs one JSON handler on `larkfield.api`, `uvicorn`,
`uvicorn.error` and `uvicorn.access`. `LOG_LEVEL` controls the level (default `INFO`). Each line is
one JSON object:

```json
{"timestamp":"2026-11-11T14:16:07.289324+00:00","level":"INFO","logger":"larkfield.api",
 "event":"ticket_processed","ticket_id":"T001","run_id":"run_...","ticket_type":"REFUND",
 "outcome":"PEND","state":"AWAITING_APPROVAL","approver_role":"SUPPORT_LEAD",
 "amount_usd":210.0,"classification_path":"pattern","latency_ms":8,"cost_usd":0.0,
 "tokens_total":0}
```

Events emitted:

| Event | Where | Main fields |
|---|---|---|
| `app_started` | `create_app` | model configured, model id, db path, counts |
| `request` | middleware | request_id, method, path, status, latency_ms |
| `request_error` | middleware | request_id, path, latency_ms, exception |
| `run_not_found` | `GET /runs` | run_id |
| `run_fetched` | `GET /runs` | run_id, ticket_id, state, outcome |
| `ticket_reused` | `POST /tickets` | ticket_id, run_id, outcome, state |
| `ticket_processed` | `POST /tickets` | ticket_id, run_id, type, outcome, state, amount, route, latency, cost, tokens |
| `decision_reused` | decision | run_id, decision, state |
| `decision_conflict` | decision | run_id, requested, existing |
| `decision_not_waiting` | decision | run_id, state |
| `decision_recorded` | decision | run_id, decision, state, approver, action, amount |
| `ledger_listed` | `GET /ledger` | rows |

No email body, email address, token or API key is logged.

### 10.2 Swagger / OpenAPI

`create_app()` declares:

- `title`, `version`, summary, Markdown description and tag metadata;
- `docs_url="/docs"`, `redoc_url="/redoc"`, `openapi_url="/openapi.json"`;
- `response_model` on every route:
  - `/health` → `HealthResponse`
  - `/tickets`, `/runs/{run_id}`, `/runs/{run_id}/decision` → `RunResponse`
  - `/ledger` → `list[LedgerRecord]`
- `responses` entries for 404/409/422/500 with `ErrorResponse`.

`src/api_models.py` extends the supplied contract models with the service's added fields
(`action`, `state`, `decisions`, `decision`, trace extras, ledger approver/decided_at). This keeps
`schema/response_schema.py` untouched while giving Swagger a complete schema.

---

## 11. Dry run

`python -m scripts.dry_run` drives the real app through `TestClient` with a temporary database and
no model key. It checks 20 invariants:

- health body exact;
- OpenAPI paths and `RunResponse` reference;
- Swagger UI served;
- T001 → PEND / `AWAITING_APPROVAL` / $210 / `SUPPORT_LEAD`;
- run fetch, request headers and trace fields;
- one proposed ledger row;
- approve → `APPROVED`, repeat idempotent, conflicting decision 409;
- ledger row updated in place;
- T002 → PROCEED / $129 / one ledger row and same run on re-post;
- T020 injection → ESCALATE / `TRUST_SAFETY` / no amount;
- lifecycle JSON logs include `ticket_processed`, `decision_recorded`, `decision_conflict`,
  `ledger_listed`, with run context.

Expected tail:

```
20/20 checks passed
```

---

## 12. Tests and evaluation

```bash
python -m pytest -q                 # 15 passed
python -m scripts.dry_run           # 20/20 checks passed
python -m eval.run_eval --offline --label after      # 40/40
python -m eval.run_eval --offline --baseline --label before  # 37/40
```

Test files:

- `tests/test_normalize_policy.py` — order-ref variants, DD/MM dates, money formats, policy matrix,
  authority limit.
- `tests/test_api.py` — health, full run, decision idempotency, conflicting decision, ticket
  idempotency, injection, partial lines, OpenAPI/Swagger, JSON logger and headers.
- `tests/test_llm_trace.py` — fake provider proves classifier + judge tokens/cost/model are traced.

Evaluation pass rule: exact outcome, `human_approval_required`, `approver_role`, amount within
$0.01 and exact reason-code set.

---

## 13. Invariants to preserve when changing code

1. The model never computes money or writes the ledger.
2. `> $150` always PENDs to `SUPPORT_LEAD`.
3. Injection/override/chargeback/legal always ESCALATEs to `TRUST_SAFETY` with no amount.
4. `> 10 days` without movement escalates; exactly 10 does not.
5. Missing/unreadable carrier facts are `UNABLE_TO_VERIFY`, never a PASS.
6. A check that cannot complete is never PASS.
7. One run per ticket, one decision per run, one ledger row per run/action.
8. Same decision is idempotent; a different decision is 409.
9. Reply amounts and order ids must match the action.
10. Sanity or judge failure fails closed to a generic support reply with no refund.

---

## 14. Extending safely

- **New intent/pattern:** add regex to `guardrails.py`, then map it in `policy.decide()` and add a
  reply template. Add a case to `eval/cases.json` and a unit test.
- **New outcome:** update `Outcome` usage, `sanity_check()`, replies, API model `state` if needed,
  and eval expectations.
- **New provider/model:** set `LLM_BASE_URL`, `LLM_MODEL`, `LLM_API_KEY`, prices. No code change.
- **New storage:** replace `Store` behind the same methods; keep the unique keys.
- **New route:** add a response model in `api_models.py`, tag it, log one lifecycle event and add
  a contract test.

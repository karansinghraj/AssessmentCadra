# Larkfield Home — Architecture

Status: design agreed before implementation. This document is the reference for the build;
`PLAN.md` turns it into time-boxed steps.

---

## 1. Business requirements

### 1.1 What the service must do

| # | Requirement | Acceptance |
|---|---|---|
| B1 | Accept one inbound support email and return the desk decision in the contract shape | `POST /tickets` returns all run fields, valid enums, HTTP 200 |
| B2 | Classify the request | Exactly one of `REFUND, RETURN, ORDER_STATUS, DELIVERY_ISSUE, OTHER` |
| B3 | Resolve the order and customer from the data room | `resolved.order_id/customer_id`; null when not established |
| B4 | Apply written policy, not model intuition | Every check cites a policy clause; every outcome is traceable to a check |
| B5 | Stop for a human when policy says so | `PEND` + `SUPPORT_LEAD`, or `ESCALATE` + `CARRIER_CLAIMS`/`TRUST_SAFETY` |
| B6 | Never act | No refund, email or data mutation outside this service's own ledgers |
| B7 | Record actions once | One ledger row per run/action; repeated decisions are idempotent |
| B8 | Explain the decision to the customer | `draft_reply` facts and amount equal the computed decision; never sent |
| B9 | Prove cost and latency | Every response has model id, token counts, `cost_usd`; latency logged |
| B10 | Survive bad input | Missing/unreadable facts produce `UNABLE_TO_VERIFY`, never `PASS` or an auto-refund |
| B11 | Reproducible data preparation | `python -m src.prepare_data` rebuilds normalised data from untouched `data/` |
| B12 | Expose current state | `GET /runs/{id}` shows a waiting run; decision endpoint moves it |

### 1.2 Hard safety invariants (evaluated as negative cases)

1. No refund above **$150** without `PEND` + `SUPPORT_LEAD`.
2. No refund when the sender's email is not the order's customer email.
3. No refund on an order reference that does not exist; never use a "nearby" order.
4. No refund for an injection/override/chargeback/legal/pre-approval email: `ESCALATE` to `TRUST_SAFETY`.
5. No refund after the policy window (30 days, or 45 with a photo for damage/fault).
6. No repayment of an issued or already-approved line.
7. A carrier fact that cannot be read is `UNABLE_TO_VERIFY` / `CARRIER_STATUS_UNAVAILABLE`, never a pass.
8. A failed check can never be `PASS`.

### 1.3 Non-functional targets

- p50 end-to-end < 2.0 s, p95 < 5.0 s (one model call, deterministic engine).
- Median model cost < $0.002 per ticket (small extraction call + templates).
- 100 % of responses schema-valid; 100 % of policy decisions reproducible without the LLM.
- Availability over correctness: if the LLM is down, rule-based fallback still returns a safe decision.

### 1.4 Policy precedence (decided)

**RP-3.2 is authoritative.** It is effective 2026-07-01 and explicitly replaces earlier guidance.
`help_centre_faq_v2.md` (published 2025-10-14) is superseded: its 14-day return window, $250
auto-refund ceiling and 5-day tracking rule are **not** used. `warehouse_picking_WH-PICK-1.4.md`
is internal warehouse procedure and sets no customer-care rule. This precedence is itself a check
(`POLICY_PRECEDENCE`) so the decision is auditable.

---

## 2. API contract

### 2.1 Endpoints

| Method | Path | Body | Response |
|---|---|---|---|
| GET | `/health` | — | `200 {"status":"ok"}` exactly |
| POST | `/tickets` | `Ticket` (schema/response_schema.py) | `TicketResponse` (run fields) |
| GET | `/runs/{run_id}` | — | current run, including waiting runs |
| POST | `/runs/{run_id}/decision` | `{"decision":"approve"\|"reject","approver":"string"}` | updated run |
| GET | `/ledger` | — | `LedgerEntry[]` |

Status codes: `404` unknown run, `409` decision on a run that is not waiting, `422` malformed body,
`503` only if the data build is missing and cannot be rebuilt.

### 2.2 Run model

The `TicketResponse` fields are returned exactly as named in the contract. Additional fields
(documented in `README.md`):

- `state`: `AWAITING_APPROVAL | APPROVED | REJECTED | ESCALATED | CLOSED` — lifecycle, separate from `outcome`.
- `decisions`: `[{decision, approver, decided_at}]` — audit history.
- `action`: `{action, amount_usd, line_no, item, status}` — the proposed/recorded action.
- `trace.latency_ms`, `trace.tokens_total`, `trace.pricing`,
  `trace.classification_path`, `trace.judge`, `trace.calls_detail` — cost/latency/route proof.

**Outcome is preserved after a decision** (`PEND` remains the desk decision that was proposed);
`state` changes to `APPROVED`/`REJECTED` and `human_approval_required` becomes `false`. This keeps
the original policy decision auditable while exposing the current state, as the contract asks.

### 2.3 Idempotency

- `POST /tickets`: `run_id` is generated once per `ticket_id`. Re-posting the same `ticket_id`
  returns the existing run; it does not create a second ledger row.
- `POST /runs/{id}/decision`: first decision wins. Same decision repeated returns the same run with
  no new ledger row. A conflicting second decision returns `409`.
- Ledger uniqueness: `UNIQUE(run_id, action)`. Approval updates the existing `PROPOSED` row; it
  never inserts a second `APPROVED` row.

### 2.4 Ledger semantics

| Situation | action | amount_usd | initial status |
|---|---|---|---|
| Refund ≤ $150, auto-proceed | `REFUND` | computed | `APPROVED` (policy-approved, still not executed) |
| Refund > $150, waiting | `REFUND` | computed | `PROPOSED` |
| PEND later approved / rejected | `REFUND` | unchanged | `APPROVED` / `REJECTED` |
| Refund refused (window, prior credit, mismatch, injection) | `REFUND` | `0.0` or null | `REJECTED` |
| Information / status reply | `REPLY` | null | `APPROVED` |
| Escalation to a team | `ESCALATION` | null | `PROPOSED` |

`recorded_at` is UTC ISO-8601. Added `approver` and `decided_at` fields are null until a decision.

---

## 3. Architecture

### 3.1 Shape

```
email ticket
    │
    ▼
FastAPI  POST /tickets ──────────────────────────────────────────────┐
    │  1. validate + normalise (email, order ref, dates, money)      │
    │  2. pattern router / guardrails (intent, refs, injection)      │
    │  3. optional LLM classifier only when route = low confidence   │
    │  4. deterministic policy engine over prepared data             │
    │  5. deterministic sanity gate (fail closed)                    │
    │  6. optional final LLM judge (can only block, never rewrite)   │
    │  7. template reply composer (facts from the decision)          │
    │  8. persist run + ledger row (SQLite, transaction)             │
    └─────────────────────────────────────────────────────────────┘  │
                                                                      │
GET /runs/{id} ◄── run store        GET /ledger ◄── ledger store     │
POST /runs/{id}/decision ──► conditional update, idempotent ─────────┘
```

**Why this shape:** the common email shapes are handled by a pattern router without a model call.
The LLM is only an ambiguity resolver and a final safety judge — it never computes money and never
writes the ledger. Money, eligibility, authority and escalation are a deterministic table over
prepared CSV data. That makes the safety invariants testable, keeps median cost near zero for simple
mail, and lets the service work when the model is unavailable. The LLM never sees or edits the ledger.

### 3.2 Components

| Component | File (planned) | Responsibility |
|---|---|---|
| Data preparation | `src/prepare_data.py` | Parse/clean/join CSVs, write `build/data.json` + manifest |
| Normaliser | `src/normalize.py` | Email, order-ref, date, money, product, claim flags |
| Pattern router / guardrails | `src/guardrails.py` | Intent/entity/injection patterns + deterministic sanity gate |
| LLM extractor + judge | `src/extract.py` | Optional strict-JSON classifier and final judge; trace + fallback |
| Policy engine | `src/policy.py` | Checks, amounts, reason codes, outcome, approver |
| Reply composer | `src/replies.py` | Deterministic customer-facing body per outcome |
| Store | `src/store.py` | SQLite runs, decisions, ledger; idempotency |
| Pipeline | `src/pipeline.py` | Pattern → optional LLM → policy → sanity → judge → trace |
| API | `src/app.py` | Contract endpoints, error mapping, structured logging |
| Middleware | `src/middleware.py` | Request id, timing and JSON access logs |
| Eval | `eval/run_eval.py` | Cases, pass rule, metrics, timestamped results |

### 3.3 Request flow (refund example)

1. Normalise sender (`trim/lower`), extract order ref from `LF-100200`, `LF100201`, `#100202`,
   `order 100203`, `order order 100204`; extract product/claim/photo.
2. LLM returns `{ticket_type, order_ref, product, claim_type, injection, confidence}`. Deterministic
   regex validates/overrides entities; a failed call falls back to rules and is flagged in trace.
3. Resolve customer by normalised email; resolve order by normalised id.
4. Run checks in order; each check returns `PASS/FAIL/NOT_APPLICABLE/UNABLE_TO_VERIFY` + clause + evidence.
5. Compute affected lines, quantity cap, prior ISSUED refunds (plus approved proposals in this service),
   amount = `unit_price × affected_qty − prior_refunds`, never negative.
6. Apply decision table (§4) → outcome, reason codes, approver.
7. Compose reply, persist run + one ledger row, return.

### 3.4 Failure modes

| Failure | Behaviour |
|---|---|
| LLM timeout/error/invalid JSON | Rule fallback; trace model `rule-based-fallback`, 0 tokens, cost 0; no PASS on unverifiable facts |
| Missing shipment / `UNAVAILABLE` | `UNABLE_TO_VERIFY`, `CARRIER_STATUS_UNAVAILABLE` |
| Conflicting duplicate order rows | Quarantine order in prep; runtime `UNABLE_TO_VERIFY`, never guess |
| Ambiguous order/date/money | Normalise with documented rule; if still ambiguous, `UNABLE_TO_VERIFY` |
| Multiple orders or conflicting asks in one email | `UNABLE_TO_VERIFY`, human review, no refund |
| DB write failure | 5xx, no partial ledger row (single transaction) |

---

## 4. Policy engine

### 4.1 Check catalogue

| check_id | Result that matters | Clause |
|---|---|---|
| `POLICY_PRECEDENCE` | RP-3.2 over FAQ/warehouse | RP-3.2 header |
| `TICKET_INJECTION` | FAIL when override/chargeback/legal/pre-approval | §5.2 |
| `CUSTOMER_IDENTITY` | PASS when email on file | §4.1 |
| `ORDER_EXISTS` | PASS when order id resolves | §4.1 |
| `ORDER_OWNERSHIP` | PASS when order belongs to sender | §4.1 |
| `ITEM_IN_ORDER` | PASS when named item is an order line | §2.2/2.4 |
| `DELIVERY_SCAN` | PASS when delivered date present | §2.1 |
| `RETURN_WINDOW` | PASS within 30 days (45 with photo + damage) | §2.1/2.3/2.6 |
| `DAMAGE_PHOTO_EXCEPTION` | PASS for damage + photo on days 31–45 | §2.6 |
| `AFFECTED_QUANTITY` | PASS when qty ≤ ordered qty | §2.5 |
| `PRIOR_REFUND` | PASS when no issued/approved credit on line | §2.5 |
| `REFUND_AMOUNT` | PASS when amount is the unpaid affected-line value | §2.2/2.5 |
| `AUTHORITY_LIMIT` | PASS at ≤ $150 | §3.1/3.2 |
| `CARRIER_MOVEMENT` | PASS when moving; FAIL when > 10 days no scan | §5.1 |
| `CARRIER_STATUS` | UNABLE when `UNAVAILABLE`/missing | §4.2 |
| `DUPLICATE_ACTION` | PASS when no approved proposal for the same line | §3.3 |

Every check carries `policy_clause`; evidence entries use
`source = data/customers.csv|data/orders.csv|data/shipments.csv|data/refunds.csv|policies/...`,
`ref = row key|clause`, `value = observed value`.

### 4.2 Decision precedence (first match wins)

1. **Injection / override / chargeback / legal / claimed pre-approval** → `ESCALATE`, approver
   `TRUST_SAFETY`, reason `POLICY_OVERRIDE_ATTEMPT`. No refund proposed.
2. **Sender not identifiable / order missing / order not sender's** → `INVALID_REQUEST`,
   `ORDER_NOT_FOUND` or `CUSTOMER_MISMATCH`. Never use a similar order.
3. **Non-delivery claim and carrier status missing/`UNAVAILABLE`** → `UNABLE_TO_VERIFY`,
   `CARRIER_STATUS_UNAVAILABLE`.
4. **No carrier movement > 10 days** → `ESCALATE`, `CARRIER_CLAIMS`, `LOST_IN_TRANSIT`.
   Exactly 10 days is **not** "more than 10" and is answered as a status.
5. **Refund/return eligibility:**
   - outside window → `REJECT`, `OUT_OF_WINDOW`;
   - all affected value already repaid → `REJECT`, `ALREADY_REFUNDED`;
   - amount > $150 → `PEND`, `SUPPORT_LEAD`, `APPROVAL_REQUIRED`;
   - otherwise `PROCEED`, or `PROCEED_MODIFIED` when the request was narrowed
     (whole order/shipping/quantity) with `PARTIAL_LINES`.
6. **Normal order/delivery status** → `PROCEED` with a status reply.
7. **Other** → `PROCEED` with a clarification reply; `UNABLE_TO_VERIFY` if a needed fact is absent.

Reason codes are additive: `DAMAGE_EXCEPTION + APPROVAL_REQUIRED` (T001),
`ALREADY_REFUNDED + APPROVAL_REQUIRED` (T026), `PARTIAL_LINES` (T019).

### 4.3 Amount rules

- Affected line value = `unit_price × affected_qty`, affected qty capped at ordered qty.
- Subtract all `ISSUED` refunds for that line, plus this service's `APPROVED` refund proposals for
  the same line (prevents a forwarded duplicate from paying twice).
- Never repay more than the unpaid affected-line value; never below zero.
- Delivery/shipping charges are not order-line value and are not refunded (§2.2); requesting them
  makes the outcome `PROCEED_MODIFIED` + `PARTIAL_LINES`.
- Authority is per request, on the final computed amount, not the order total or the claimed amount.

### 4.4 Window rules

- Age = `received_at (UTC date) − delivered_at (UTC date)`.
- Days ≤ 30: eligible.
- Days 31–45 **and** damage/fault **and** an image attachment: eligible; add `DAMAGE_EXCEPTION`.
- Otherwise > 30 → `OUT_OF_WINDOW`. Wrong/not-as-described without damage never gets the extension.
- 45+ → `OUT_OF_WINDOW` even with a photo.
- No delivery scan → `UNABLE_TO_VERIFY` for a return/refund; do not use order date as a substitute.

### 4.5 Data preparation rules

- Never edit `data/`. One command: `python -m src.prepare_data`.
- Order ids: strip `LF-`, `LF`, `#`, spaces, leading text; zero-pad to 6 digits; canonical `LF-######`.
- Dates: ISO first; slash dates are **DD/MM/YYYY** (proved by unambiguous values `26/08`, `27/10`,
  `30/09` in the same export). Record the rule in the manifest.
- Money: strip `$`, `USD`, commas, whitespace; parse Decimal, store cents.
- Duplicate order rows: collapse only when identical; conflicting duplicates are quarantined.
- Shipment scans before `shipped_at` are data anomalies; preserve them, flag the check evidence, and
  use the later of `last_event_at`/`shipped_at` only for presentation, not to hide the anomaly.
- Refunds: group by `(order_id, line_no)` and sum only `ISSUED`.
- Emit `build/manifest.json` with source SHA-256 hashes and counts; generated output is deterministic.

### 4.6 Data findings so far

- 53 customers, 49 order rows → **47 unique orders**, 57 order lines, 47 shipments, 3 issued refunds.
- 2 duplicate order rows (`LF-100216`, `LF-100239`), identical values.
- 13 non-ISO dates (`DD/MM/YYYY`); 17 `$` totals + 16 `USD ` totals.
- 3 `UNAVAILABLE` shipments (`LF-100212`, `LF-100232`, `LF-100238`).
- 4 shipments with a last scan before `shipped_at` (`LF-100210/211/229/239`).
- 3 issued refunds, including a fully refunded line (`LF-100217` line 1) and a $45 vase credit
  (`LF-100224` line 1).
- No duplicate customer emails; no order-total vs line-sum mismatch after normalisation.

---

## 5. Database

### 5.1 Prepared read model (immutable at runtime)

`build/data.json` — loaded into memory at startup; rebuilt by one command:

```json
{
  "customers": {"C10000": {"email": "...", "name": "...", "tier": "silver"}},
  "customers_by_email": {"asha.shah0@mail.example": "C10000"},
  "orders": {"LF-100200": {"customer_id": "C10000", "status": "DELIVERED",
              "shipping_fee_cents": 0, "total_cents": 21000, "lines": [...]}},
  "shipments": {"LF-100200": {"status": "DELIVERED", "delivered_at": "...", "last_event_at": "..."}},
  "refunds": {"LF-100217": {"1": 18500}},
  "manifest": {"source_hashes": {...}, "counts": {...}, "rules": {...}}
}
```

### 5.2 Runtime store (SQLite, WAL)

```sql
runs(
  run_id TEXT PRIMARY KEY,
  ticket_id TEXT UNIQUE NOT NULL,
  ticket_json TEXT NOT NULL,
  response_json TEXT NOT NULL,      -- last returned contract body
  state TEXT NOT NULL,              -- AWAITING_APPROVAL/APPROVED/REJECTED/CLOSED
  outcome TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

decisions(
  id INTEGER PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES runs(run_id),
  decision TEXT NOT NULL,
  approver TEXT NOT NULL,
  decided_at TEXT NOT NULL,
  UNIQUE(run_id)                    -- first decision wins
);

ledger(
  run_id TEXT NOT NULL REFERENCES runs(run_id),
  ticket_id TEXT NOT NULL,
  action TEXT NOT NULL,             -- REFUND/REPLY/ESCALATION
  amount_usd REAL,
  status TEXT NOT NULL,             -- PROPOSED/APPROVED/REJECTED
  recorded_at TEXT NOT NULL,
  approver TEXT,
  decided_at TEXT,
  PRIMARY KEY(run_id, action)
);
```

Why SQLite: zero external dependency, transactional idempotency, sufficient for review traffic.
The ledger primary key enforces "recorded once". On a single instance WAL gives safe concurrent reads.
Limitation: free-host ephemeral disk (documented); move to Postgres for production.

---

## 6. AI usage, cost and latency

- **Model calls are conditional:** the pattern router handles common emails with no call. The
  classifier is called only when confidence is low. The final judge is called when a key is
  configured. No chain-of-thought, no tool loop, no model-authored money.
- Provider-agnostic OpenAI-compatible endpoint via env (`LLM_BASE_URL`, `LLM_MODEL`,
  `LLM_API_KEY`). The deployed service uses its **own** key; the coding-agent token is never shipped.
- Request: ticket subject/body + normalised pattern hints + compact enums (~700–1,000 input tokens);
  classifier response ≤ 260 tokens; judge response ≤ 160 tokens.
- **Cost** = `input_tokens × input_price + output_tokens × output_price`, prices in `src/config.py`
  (env override). Per-call usage is read from the provider response, never estimated.
- **Latency** measured around each call and around the whole request; stored as
  `trace.latency_ms`, `trace.calls_detail[].latency_ms` and logged.
- **Reply text is deterministic** from the decision, so amounts cannot drift. The judge can only
  block a run (fail closed to `UNABLE_TO_VERIFY`), never rewrite the amount or outcome.
- **Fallback**: 8 s timeout, one retry, then rule-based path with `model="rule-based-fallback"`,
  zero tokens/cost; the policy decision remains valid.
- Budget: median target < $0.002/ticket; hard cap per request via `max_tokens` and body length.
  Pattern-only tickets cost $0.000000; a judge-only ticket is roughly $0.0002 at the default prices.

---

## 7. Observability and evaluation

### 7.1 Observability

- One JSON access log line per request: `request_id, method, path, status, latency_ms`; no email body
  is logged.
- Trace returned in every response: `model_calls[]`, `tools_called[]`, `documents_used[]`, `cost_usd`,
  plus `latency_ms`, `tokens_total`, `pricing`, `classification_path`, `judge`, `sanity_issues`,
  `calls_detail`. Every run is reconstructable from `runs.response_json`.
- No raw card/payment data exists in the data room; emails are not written to logs.
- Optional `/metrics` (not required by contract) can expose counters; core proof stays in the trace.

### 7.2 Evaluation

- `eval/cases.json`: 15 senior-agent labelled tickets + ≥ 6 new negative cases (≥ 3 kinds):
  injection on an above-limit order, email/order mismatch, non-existent order, damaged day 40
  without photo, carrier `UNAVAILABLE`, duplicate refunded line, $150 boundary, 10-day boundary,
  order-id/email formatting variants.
- **Pass rule**: outcome exact **and** approval flag exact **and** approver role exact **and**
  amount within $0.01 **and** reason-code set exact. A case failing any field fails.
- `python -m eval.run_eval --out eval/results/<timestamp>.json` runs in-process (TestClient) and
  writes per-case diffs, pass rate, median/p95 latency, median cost.
- Contract tests in `tests/`: health exact body, schema validity, waiting run fetch, double-decision
  idempotency, ledger length stable, no refund above $150 without PEND.
- Two committed timestamped result files: `..._before.json` and `..._after.json`, with the one change
  and its effect documented in `ARTEFACT.md`.

### 7.3 Evaluation cadence

1. Baseline after engine exists but before the 45-day photo exception (expect T001/P01 failures).
2. Add `DAMAGE_EXCEPTION` window logic; rerun; commit `..._after.json`.
3. Safety suite must be 100 %; any regression blocks deployment.

---

## 8. Deployment

- `Dockerfile` (python:3.12-slim) + `render.yaml`; start:
  `uvicorn src.app:app --host 0.0.0.0 --port $PORT`.
- Build step: `python -m src.prepare_data`; runtime auto-builds if `build/data.json` is absent.
- Env: `PORT`, `LLM_API_KEY`, `LLM_BASE_URL`, `LLM_MODEL`, `LLM_INPUT_USD_PER_MTOK`,
  `LLM_OUTPUT_USD_PER_MTOK`, `RUN_DB_PATH`, `DATA_BUILD_DIR`.
- Never commit `.env` or keys. `.env.example` only.
- Health check path `/health`; free-tier cold start accepted, documented.

---

## 9. Unfinished / out of scope

- No actual refund execution, email sending, or carrier API calls (by design and by contract).
- Attachment contents are not analysed; presence of an image is sufficient per RP-3.2 §2.6.
- No authentication on approver identity (contract supplies a name only).
- Single-instance SQLite; no queue/retry for downstream processing.
- Multi-order or multi-intent emails return `UNABLE_TO_VERIFY` for human review rather than split.
- Customer identity is email-match only; spoofing/account takeover is out of scope.
- Free-host ephemeral storage may lose runs on restart; production would use Postgres.

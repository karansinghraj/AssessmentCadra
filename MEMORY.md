# PROJECT MEMORY — Larkfield Home email triage

Read this first. It is the current state of the build; you should not need to re-read every
source file to continue.

Last updated: 2026-11-11 (implementation, eval and docs complete; deployment pending).

---

## 1. What this is

An HTTP service that takes one customer-support email and returns the desk's decision. It
**proposes only** — it never refunds, sends email, or mutates customer data.

Contract (exact fields in `schema/response_schema.py`):

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | exact `{"status":"ok"}` |
| POST | `/tickets` | one ticket → full run |
| GET | `/runs/{run_id}` | current run, including waiting |
| POST | `/runs/{run_id}/decision` | `{"decision":"approve"\|"reject","approver":"..."}` |
| GET | `/ledger` | recorded actions |

Service reads `PORT`, uses an OpenAI-compatible provider configured by env.

---

## 2. Current status

- Data preparation: **done**, one command, deterministic.
- Pattern router + guardrails: **done**.
- Optional LLM classifier + final LLM judge: **done** (needs `LLM_API_KEY` to run).
- Deterministic policy engine: **done**.
- SQLite runs/decisions/ledger with idempotency: **done**.
- FastAPI endpoints + middleware: **done**.
- Swagger/OpenAPI (`/docs`, `/redoc`, `/openapi.json`) and JSON logger: **done**.
- End-to-end dry run: **20/20 checks passed** (`python -m scripts.dry_run`).
- Unit/contract tests: **15 passed** (`python -m pytest -q`).
- Offline eval: **40/40 pass (100%)** after; **37/40 (92.5%)** in baseline without the
  45-day photo exception. See `eval/results/`.
- Deployment: **not done yet** (need a public host + own model key).
- Git: committed locally (not pushed); `opencode.json` is untracked and ignored because it holds a
  coding-agent token — never commit it.
- `APPROACH.md` (250 words) and `ARTEFACT.md`: **done**; deployment evidence still pending.

---

## 3. File map

| File | What it does |
|---|---|
| `src/config.py` | env config, model prices, cost calculation |
| `src/normalize.py` | email/order/date/money/text normalisation |
| `src/prepare_data.py` | reads `data/*.csv` untouched → `build/data.json` + manifest |
| `src/guardrails.py` | pattern router, injection patterns, deterministic sanity gate |
| `src/extract.py` | optional LLM classifier + final judge, JSON parsing, model-call trace |
| `src/policy.py` | checks, amount, window, authority, escalation, decision table |
| `src/replies.py` | deterministic customer reply templates |
| `src/store.py` | SQLite runs, decisions, ledger, idempotency |
| `src/pipeline.py` | pattern → optional LLM → policy → sanity → judge → trace |
| `src/app.py` | FastAPI routes, OpenAPI metadata, response models, route logs |
| `src/api_models.py` | Swagger-only response models (contract + added fields) |
| `src/logging_config.py` | JSON formatter and API/uvicorn logger setup |
| `src/middleware.py` | request id, timing, JSON access logs |
| `scripts/dry_run.py` | end-to-end 20-check flow validation, no model key |
| `eval/run_eval.py` | 40-case runner, pass rule, timestamped results |
| `eval/cases.json` | expected labels, public expected, 15 custom cases |
| `tests/` | normaliser/policy, API contract/idempotency, LLM trace |
| `ARCHITECTURE.md` | full design |
| `CODE.md` | code structure and functional logic |
| `PLAN.md` | time-boxed build plan |
| `MEMORY.md` | this file |

Generated/ignored: `build/`, `var/`, `*.db`, `.venv/`.

---

## 4. Pipeline in one screen

```
ticket
  → guardrails.analyse()            # patterns: intent, order refs, damage, injection
  → if low confidence and key: extract.classify()   # LLM fills ambiguity
  → policy.decide()                 # deterministic checks + money + outcome
  → guardrails.sanity_check()       # fail closed on invariant breach
  → extract.judge() if key          # final LLM safety judge; can only block
  → trace + SQLite run/ledger
```

**Important:** the LLM never computes money and never writes the ledger. If no key is configured,
the service still returns a safe rule-based decision and records `rule-based-fallback` with 0 tokens.

---

## 5. Policy decisions you must not break

1. **RP-3.2 v3.2 is authoritative.** FAQ v2 is superseded; warehouse doc out of scope.
2. Window: ≤30 days; 31–45 days only for damage/fault **with an image**; >45 reject.
3. Amount: `unit_price × affected_qty`, capped by ordered qty; subtract `ISSUED` refunds and
   approved local proposals; never below 0; shipping excluded.
4. Authority: > **$150** → `PEND` + `SUPPORT_LEAD` + `APPROVAL_REQUIRED`.
5. Injection/chargeback/legal/pre-approval → `ESCALATE` + `TRUST_SAFETY`, no refund.
6. No carrier movement **> 10 days** → `ESCALATE` + `CARRIER_CLAIMS`; exactly 10 is not lost.
7. Missing/`UNAVAILABLE` carrier → `UNABLE_TO_VERIFY` + `CARRIER_STATUS_UNAVAILABLE`.
8. Invalid sender/order → `INVALID_REQUEST` (`ORDER_NOT_FOUND` / `CUSTOMER_MISMATCH`).
9. Whole order/shipping when only a line is eligible → `PROCEED_MODIFIED` + `PARTIAL_LINES`.
10. A check that cannot complete is **never PASS**.
11. Outcome stays `PEND` after approval; added `state` becomes `APPROVED`/`REJECTED`; ledger row
    is updated once, never duplicated.
12. Added response fields: `action`, `state`, `decisions`, `trace.latency_ms`,
    `trace.tokens_total`, `trace.pricing`, `trace.classification_path`, `trace.judge`.

State values: `AWAITING_APPROVAL | APPROVED | REJECTED | ESCALATED | CLOSED`.

Ledger actions: `REFUND | REPLY | ESCALATION`; statuses `PROPOSED | APPROVED | REJECTED`.
Added ledger fields: `approver`, `decided_at`.

---

## 6. Data facts (do not re-derive)

- 53 customers, 49 order rows → **47 unique orders**, 57 lines, 47 shipments, 3 issued refunds.
- Duplicate order rows: `LF-100216`, `LF-100239` (identical, collapsed).
- 13 slash dates are `DD/MM/YYYY`; 17 `$` totals, 16 `USD ` totals.
- `UNAVAILABLE` shipments: `LF-100212`, `LF-100232`, `LF-100238`.
- Scan-before-ship anomalies: `LF-100210`, `LF-100211`, `LF-100229`, `LF-100239`.
- Prior refunds: `LF-100217#1` $185, `LF-100224#1` $45, `LF-100243#1` $45.
- Order refs in mail appear as `LF-100200`, `LF100201`, `#100202`, `order 100203`,
  `order order 100204`; all normalise to `LF-######`.

---

## 7. Commands

```bash
# data (must be reproducible; sources are never edited)
python -m src.prepare_data --report

# tests + dry run
python -m pytest -q
python -m scripts.dry_run

# evaluation (offline, no model key)
python -m eval.run_eval --offline --label after
python -m eval.run_eval --offline --baseline --label before

# run locally
uvicorn src.app:app --host 0.0.0.0 --port 8000 --no-access-log
```

Model env for deployment: `LLM_API_KEY`, `LLM_BASE_URL`, `LLM_MODEL`,
`LLM_INPUT_USD_PER_MTOK`, `LLM_OUTPUT_USD_PER_MTOK`, `LOG_LEVEL`. Never commit the key.

---

## 8. Eval numbers

- Result files currently in `eval/results/`:
  - `20261111T141802Z_before.json` — 37/40 (92.5%), failures T001, P01, N12.
  - `20261111T141806Z_after.json` — 40/40 (100%).
- Change: enabled the 31–45 day damage/photo exception (`DAMAGE_EXCEPTION`).
- Offline median latency <1 ms (no model), median cost $0.000000; deployed model path is traced
  per request with provider token usage.
- Contract checks pass: health exact, waiting run fetch, approve twice → one APPROVED ledger row,
  conflicting decision 409, same ticket → same run and one ledger row.

---

## 9. Known gaps / next steps

1. Deploy to Render (Docker) with own `LLM_API_KEY`; verify real model trace + token counts.
2. Run the eval against the deployed URL if a provider key is available; record real cost/latency.
3. Update `ARTEFACT.md` and `MEMORY.md` with the deployed URL and real-provider cost/latency.
4. Optional: split routes into `APIRouter` modules if the file grows.
5. Optional: replace SQLite with Postgres for multi-instance production.
6. Free-host ephemeral storage may lose runs on restart; `RUN_DB_PATH` points at `/tmp` on Render.

# Artefact — Larkfield Home support email triage

This is the written deliverable. It records what the data showed, the policy I applied, how the
service is built, the measured evaluation delta, cost/latency evidence, and what is deliberately
not built. Repro commands are at the end.

---

## 1. What was built

An HTTP service (`src/app.py`) that turns one support email into a desk decision. It **proposes
only**: it never refunds, sends email, or mutates customer data. One action row is written per
action, decisions are idempotent, and the service reads `PORT` from the environment.

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | exact `{"status":"ok"}` |
| POST | `/tickets` | ticket → full run (contract fields plus additions) |
| GET | `/runs/{run_id}` | current run, including `AWAITING_APPROVAL` |
| POST | `/runs/{run_id}/decision` | `{"decision":"approve"\|"reject","approver":"..."}` |
| GET | `/ledger` | recorded action rows |

Pipeline: pattern router → optional LLM classifier for low-confidence mail → deterministic policy
engine → deterministic sanity gate (fail closed) → optional LLM judge (block-only) → template
reply → SQLite run + ledger. The LLM never computes money and never writes the ledger. Without a
provider key the service still returns a safe rule-based decision and traces
`model="rule-based-fallback"` with zero tokens.

---

## 2. Data findings and what I did

The export was dirty by design. Every fix is in `src/prepare_data.py`, is deterministic, and leaves
`data/*.csv` untouched. A SHA-256 manifest (`build/manifest.json`) records the source state.

| Finding | Count / ids | Action |
|---|---|---|
| Duplicate order rows | `LF-100216`, `LF-100239` | Identical rows collapsed to one order; conflicting rows would be quarantined |
| Order reference variants | `LF-100200`, `LF100201`, `#100202`, `order 100203`, `order order 100204` | Normalised to canonical `LF-######` |
| Slash dates | 13 values, proved `DD/MM/YYYY` by days > 12 | Parsed day-first; ISO dates kept as-is |
| Money formats | `$129.00` (17), `USD 24.00` (16), plain (16) | Stripped to integer cents |
| Shipments `UNAVAILABLE` | `LF-100212`, `LF-100232`, `LF-100238` | Never guessed; run returns `UNABLE_TO_VERIFY` / `CARRIER_STATUS_UNAVAILABLE` |
| Scans before `shipped_at` | `LF-100210`, `LF-100211`, `LF-100229`, `LF-100239` | Flagged in the manifest; carrier facts treated as unverifiable where they drive an outcome |
| Issued refunds | `LF-100217#1` $185, `LF-100224#1` $45, `LF-100243#1` $45 | Subtracted from any new amount; duplicate claims rejected or credited correctly |
| Totals vs lines | 47 orders, 57 lines | Line-level maths used for refunds; order total used only to detect partial-line requests |

Prepared counts: **53 customers, 49 order rows → 47 unique orders, 57 lines, 47 shipments, 3
issued refunds.**

---

## 3. Policy choice

`policies/refund_policy_RP-3.2.md` is authoritative (version 3.2, effective 2026-07-01, explicitly
replaces earlier guidance). `help_centre_faq_v2` is superseded and used only as a precedence check.
The warehouse-picking document does not set customer-care rules.

Applied rules:

- **Window:** ≤30 days from carrier delivery; 31–45 days only for damage/fault **with an image
  attachment**; otherwise `REJECT` / `OUT_OF_WINDOW`.
- **Amount:** affected `unit_price × quantity`, capped at ordered quantity, minus `ISSUED` refunds
  and locally approved proposals; never negative; shipping excluded.
- **Authority:** above **$150** → `PEND` + `SUPPORT_LEAD` + `APPROVAL_REQUIRED`.
- **Override attempts / chargeback / legal / claimed pre-approval:** `ESCALATE` +
  `TRUST_SAFETY`; no refund.
- **Transit:** no carrier movement **> 10 days** → `ESCALATE` + `CARRIER_CLAIMS`; exactly 10 days
  is not lost.
- **Missing/unreadable carrier:** `UNABLE_TO_VERIFY` + `CARRIER_STATUS_UNAVAILABLE`; never a pass.
- **Invalid sender/order:** `INVALID_REQUEST` + `CUSTOMER_MISMATCH` / `ORDER_NOT_FOUND`; never act
  on a nearby order.
- **Whole-order or shipping ask when only a line is eligible:** `PROCEED_MODIFIED` +
  `PARTIAL_LINES`.
- **Unknown/unresolvable:** generic support reply, no action.

I chose a deterministic money engine over an LLM because the safety invariants (limit, window,
duplicate refund, escalation) must be testable and stable. The LLM is confined to reading ambiguous
mail and to a final block-only judge.

---

## 4. Evaluation

`eval/run_eval.py` drives the real FastAPI app (TestClient) over **40 cases**:

- 15 tickets labelled by a senior agent (`tickets/eval_seed_labelled.json`);
- 10 public tickets (`inbox/public_tickets.json`);
- 15 new cases, including **10 negatives across 8 kinds** (injection, identity mismatch,
  nonexistent order, missing order reference, unreadable carrier, duplicate refund, window/photo
  edge, ambiguous multi-order) plus boundaries ($150 limit, 45-day photo, 46-day photo, 10-day
  transit, prior refund on another line) and format variants.

**Pass rule:** outcome, `human_approval_required`, `approver_role`, amount within $0.01, and the
exact reason-code set must all match.

| Run | Result | Failures |
|---|---|---|
| Before (45-day damage/photo exception disabled) | **37/40 (92.5%)** | `T001`, `P01`, `N12` |
| After (exception enabled) | **40/40 (100%)** | none |

Change: a damaged/faulty item with an image is accepted at 31–45 days and tagged
`DAMAGE_EXCEPTION`, per RP-3.2 §2.6. Committed results:

- `eval/results/20261111T141802Z_before.json`
- `eval/results/20261111T141806Z_after.json`

Contract checks pass in both runs: health body exact; a waiting run is fetchable; approving twice
leaves **one** `APPROVED` ledger row; a conflicting decision returns **409**; posting the same
ticket twice returns the same `run_id` and one ledger row.

`python -m pytest -q` → **17 passed** (normalisation, policy, API contract, idempotency, LLM trace,
Swagger/OpenAPI, JSON logging, serverless config). `python -m scripts.dry_run` → **20/20 checks
passed**. `python -m scripts.vercel_smoke` → **10/10 checks passed** (database and prepared data under
the OS temp directory, Vercel-style entrypoint).

---

## 5. Cost and latency

Measured on the final code with the deterministic path (no provider key). The measured fields are
the same fields used when a key is present; only the call count and token counts change.

| Representative ticket | Path | Latency | Cost | Tokens |
|---|---|---|---|---|
| `T002` — simple in-window refund | pattern only | **0 ms** | **$0.000000** | 0 |
| `T001` — 45-day damage-with-photo, $210 → PEND | pattern + policy | **5 ms** | **$0.000000** | 0 |
| `T019` — whole-order ask, one eligible line | pattern + policy | **1 ms** | **$0.000000** | 0 |
| **All 40 cases** | pattern/rule fallback | **median 0.0 ms (sub-ms), p95 1.0 ms** | **median $0.000000**, total $0.000000 | 0 |

Why cost is zero here: no `LLM_API_KEY` is available in this environment, so the trace records
`rule-based-fallback`. When the deployed service has its own key, the classifier is called only for
low-confidence mail and the judge on each request; token usage is read from the provider response
and priced from `LLM_INPUT_USD_PER_MTOK` / `LLM_OUTPUT_USD_PER_MTOK` in `src/config.py`, and appears
per run in `trace.cost_usd`, `trace.tokens_total` and `trace.calls_detail`. The default price table
is $0.15 / $0.60 per million input/output tokens, so a ~1k-input classifier plus ~1k-input judge
request is roughly **$0.0003**.

---

## 6. What is not built

- **No public deployment yet.** Vercel serverless config (`main.py`, `vercel.json`,
  `.python-version`, `.vercelignore`) and the Docker/Render files are ready and locally validated;
  publishing needs the user's repo connection and model-provider key. There is therefore no live URL
  and no real-provider cost/latency measurement in this artefact.
- No actual refund execution, email sending or carrier API integration (by contract).
- Attachments are a boolean signal only; image content is not analysed (RP-3.2 §2.6 treats a
  supplied image as sufficient for this desk).
- Approver identity is free text; the contract provides no authentication.
- Multi-order or conflicting-intent mail returns `UNABLE_TO_VERIFY` rather than being split.
- SQLite is single-instance; a free host's ephemeral disk may lose runs on restart. Production
  would use Postgres.
- Email matching is the identity check; spoofing and account takeover are out of scope.

---

## 7. Reproduce

```bash
python -m src.prepare_data --report
python -m pytest -q
python -m eval.run_eval --offline --baseline --label before
python -m eval.run_eval --offline --label after
uvicorn src.app:app --host 0.0.0.0 --port 8000
```

`README.md` covers env vars, added response fields and deployment; `APPROACH.md` is the ≤250-word
reviewer summary; `MEMORY.md` is the handoff state.

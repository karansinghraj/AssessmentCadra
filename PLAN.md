# Build plan — Larkfield Home email triage

Companion to `ARCHITECTURE.md`. Timebox assumes ~120 minutes, one tracked session.
Rule: **understand → walking skeleton → policy → eval → deploy → document**. Do not polish prose
before the safety suite is green.

---

## 0. Decision summary (fixed before coding)

| Question | Decision |
|---|---|
| Which policy wins? | RP-3.2 v3.2. FAQ v2 is superseded; warehouse doc is out of scope. |
| Who decides money? | Deterministic engine. LLM only extracts intent/entities and flags injection. |
| What can auto-proceed? | Refund ≤ $150 after all checks pass; information replies; status replies. |
| What must wait? | Refund > $150 → `PEND` + `SUPPORT_LEAD`. |
| What escalates? | Injection/chargeback/legal/pre-approval → Trust & Safety. No movement > 10 days → Carrier Claims. |
| What is never a PASS? | Missing/ambiguous carrier, order, identity, delivery date, amount. |
| What happens after approval? | Outcome stays `PEND`; `state` → `APPROVED`, flag false, ledger row updated once. |
| Where is state? | SQLite, transactional, unique `(run_id, action)` and unique decision per run. |
| How is data prepared? | `python -m src.prepare_data` → `build/data.json` + manifest. `data/` untouched. |
| How is cost proven? | Provider token usage × configured price; trace per response; evaluator aggregates. |

---

## 1. Phase plan (120 min)

### Phase A — Understand and decide (0–35 min) — **done / current**

- [x] Read brief, contract, response schema.
- [x] Read all three policies; resolve precedence (RP-3.2).
- [x] Profile all five CSVs; list dirty-data rules.
- [x] Read 30 inbox tickets + 15 labels + 10 public tickets.
- [x] Derive decision precedence and amount/window rules.
- [x] Write `ARCHITECTURE.md` and this plan.
- [ ] Freeze the decision table; no rule changes after Phase C without updating docs.

**Exit gate:** every labelled ticket has a hand-computed expected outcome/amount using the table.
Spot checks already done: T001 PEND 210; T019 PROCEED_MODIFIED 27.50; T026 PEND 185;
T013 UNABLE_TO_VERIFY; T018 REJECT; T020/T021 ESCALATE.

---

### Phase B — Data preparation and walking skeleton (35–55 min)

**B1. Project skeleton**
- [ ] `src/config.py` — env parsing (`PORT`, model, prices, DB path).
- [ ] `src/prepare_data.py` — CSV load, normalise, join, manifest, report.
- [ ] `src/normalize.py` — email/order/date/money/product helpers.
- [ ] `src/store.py` — SQLite schema + run/ledger/decision methods.
- [ ] `src/policy.py` — checks + decision table.
- [ ] `src/replies.py` — deterministic replies.
- [ ] `src/extract.py` — LLM JSON call + rule fallback.
- [ ] `src/app.py` — FastAPI contract.
- [ ] `requirements.txt` — `fastapi`, `uvicorn[standard]`, `pydantic>=2`, `httpx`, `pytest`.
- [ ] `Dockerfile`, `render.yaml`, `.env.example`, `.gitignore`.

**B2. One-command data build**
```bash
python -m src.prepare_data
```
Must print: counts, duplicate rows collapsed, date/money formats, event-before-ship anomalies,
`UNAVAILABLE` shipments, issued refunds, source hashes. Must be deterministic (same bytes on rerun).

**B3. Walking skeleton**
- [ ] `GET /health` returns exactly `{"status":"ok"}`.
- [ ] `POST /tickets` accepts a public ticket and returns a schema-valid response (even if outcome
      is temporarily `OTHER`).
- [ ] `GET /runs/{id}` returns the stored run.
- [ ] `GET /ledger` returns one row.
- [ ] Smoke test with `P01`.

**Exit gate:** public ticket round-trips; prepared data loads in < 1 s; DB file created.

---

### Phase C — Policy engine (55–85 min)

**C1. Normalisation**
- [ ] Trim/lower email; `LF-100200` = `LF100201` = `#100202` = `order 100203` = `order order 100204`.
- [ ] Slash dates as `DD/MM/YYYY` (documented; unambiguous values prove the format).
- [ ] Money `$129.00`, `USD 24.00`, `258.00` → cents.
- [ ] Product match against order lines; detect whole-order/shipping asks.

**C2. Checks + decision table**
- [ ] Implement check catalogue and precedence from `ARCHITECTURE.md` §4.
- [ ] Reason-code additivity (`DAMAGE_EXCEPTION`, `PARTIAL_LINES`, `ALREADY_REFUNDED`,
      `APPROVAL_REQUIRED`, `LOST_IN_TRANSIT`, `POLICY_OVERRIDE_ATTEMPT`, `CARRIER_STATUS_UNAVAILABLE`,
      `ORDER_NOT_FOUND`, `CUSTOMER_MISMATCH`, `OUT_OF_WINDOW`).
- [ ] Amount: qty cap, prior `ISSUED` refunds, approved proposals, shipping exclusion, floor at 0.
- [ ] Window: ≤30; 31–45 damage+photo; >45 reject; 10-day lost boundary strict `>`.
- [ ] `human_approval_required` / `approver_role` exactly for `PEND`/`ESCALATE`.
- [ ] Draft reply from a template with computed amount/order/item only.

**C3. LLM extractor**
- [ ] OpenAI-compatible call, strict JSON, 8 s timeout, one retry.
- [ ] Validate against allowed enums; reject unknown keys.
- [ ] Injection regex pre-filter + model flag; deterministic override.
- [ ] Fallback to rules on any error; trace model id `rule-based-fallback`, tokens 0.
- [ ] Record `model_calls`, `documents_used`, `cost_usd`, `latency_ms`.

**C4. Store + decision endpoint**
- [ ] Transaction writes run + ledger atomically.
- [ ] Unique `ticket_id` → idempotent `POST /tickets`.
- [ ] Unique decision per run; same decision idempotent; conflicting → 409.
- [ ] Approval updates existing ledger row; never inserts a second.
- [ ] `GET /runs` shows `state=AWAITING_APPROVAL`, `approver_role`.

**Exit gate:** all 15 labelled tickets produce the expected outcome/flag/role/amount by hand-check;
`pytest` unit tests for normaliser/policy/store pass.

---

### Phase D — Evaluation and one measured change (85–102 min)

**D1. Cases**
- [ ] `eval/cases.json`: 15 labelled + ≥6 new negatives, including:
  - [ ] injection with above-limit amount → `ESCALATE/TRUST_SAFETY`;
  - [ ] sender email ≠ order customer → `INVALID_REQUEST/CUSTOMER_MISMATCH`;
  - [ ] non-existent order → `INVALID_REQUEST/ORDER_NOT_FOUND`;
  - [ ] damaged day 40 **without** photo → `REJECT/OUT_OF_WINDOW`;
  - [ ] carrier `UNAVAILABLE` → `UNABLE_TO_VERIFY/CARRIER_STATUS_UNAVAILABLE`;
  - [ ] fully refunded line → `REJECT/ALREADY_REFUNDED`;
  - [ ] $150 vs $150.01 boundary;
  - [ ] exactly 10 days no movement vs 11 days;
  - [ ] order-id/email formatting variants.
- [ ] State pass rule in runner and README: outcome + approval + role + amount ±$0.01 + reason set.

**D2. Run before**
- [ ] Run against the build **without** the 45-day photo exception (or before prior-refund fix).
- [ ] Commit `eval/results/<ts>_before.json` with pass rate, failing case ids, p50/p95 latency, median cost.

**D3. One change**
- [ ] Add the missing rule (recommended: `DAMAGE_EXCEPTION` 31–45 days with image).
- [ ] Rerun; commit `eval/results/<ts>_after.json`.
- [ ] Record exact delta in `ARTEFACT.md` (e.g. 18/21 → 21/21; T001 and P01 fixed).

**D4. Contract/idempotency tests**
- [ ] Health exact body.
- [ ] POST → GET run → decision → GET run → ledger.
- [ ] Double approve → one `APPROVED` row, amount unchanged.
- [ ] Reject path → `REJECTED`, no approval.
- [ ] Re-post same ticket → same `run_id`, ledger stable.

**Exit gate:** safety cases 100 %; overall ≥ 90 %; no above-limit auto-approval.

---

### Phase E — Deploy and verify (102–113 min)

- [ ] Push public GitHub repo from the template; confirm data files unchanged.
- [ ] Create Render web service (Docker), set `PORT` automatically, add `LLM_API_KEY` +
      `LLM_BASE_URL` + `LLM_MODEL` + prices as secret env vars (never in git).
- [ ] Verify deployed: `GET /health`, `POST /tickets` with P01, `GET /runs/{id}`,
      decision on a PEND ticket, `GET /ledger`.
- [ ] Check response contains model id and token counts on the deployed service.
- [ ] Record URL and cold-start behaviour.

**Exit gate:** public URL responds; one PEND run survives fetch; ledger idempotent after two decisions.

---

### Phase F — Documents and final check (113–120 min)

- [ ] `APPROACH.md` ≤ 250 words, first-reader orientation, links to code/eval.
- [ ] `ARTEFACT.md` with real numbers:
      data findings and actions; policy used; before/after eval; 3 cost/latency lines; median cost;
      what is not built.
- [ ] `README.md`: prepare command, run command, env vars, endpoints, added fields, eval command.
- [ ] Re-run `python -m src.prepare_data`, `pytest`, eval on the final commit.
- [ ] Confirm `git status` clean, no secrets, two result files committed.
- [ ] Submit service URL + repo URL.

---

## 2. Work breakdown by workstream

| Workstream | Deliverable | Depends on | Est. |
|---|---|---|---|
| Business rules | decision table, reason-code map, policy precedence note | data room read | 20 min |
| API | 5 endpoints, schema-valid responses, error codes | store + engine | 20 min |
| Data | normaliser + one-command build + manifest/report | rules | 15 min |
| Database | SQLite schema, transactions, idempotency keys | API shape | 12 min |
| Policy | checks, amounts, windows, authority, escalation | data + rules | 25 min |
| AI | extraction prompt/schema, fallback, token/cost trace | policy enums | 15 min |
| Cost/latency | price table, latency timer, eval aggregation | AI + eval | 8 min |
| Observability | JSON logs, run trace, contract tests | API | 10 min |
| Evals | cases (≥12, ≥4 new negatives, ≥3 kinds), runner, 2 result files | engine | 20 min |
| Docs/deploy | README, APPROACH, ARTEFACT, Render | all | 15 min |

Overlap is intentional: data + policy are built together; eval cases are written while the engine
is still warm.

---

## 3. File tree to build

```
.
├── APPROACH.md                 # ≤250 words, final
├── ARTEFACT.md                 # real numbers, final
├── ARCHITECTURE.md             # this design
├── PLAN.md                     # this plan
├── README.md                   # setup, run, endpoints, added fields
├── DATA_DICTIONARY.md          # untouched
├── data/                       # untouched
├── policies/                   # untouched
├── schema/response_schema.py   # untouched
├── inbox/public_tickets.json   # untouched
├── tickets/                    # untouched
├── src/
│   ├── app.py                  # FastAPI
│   ├── config.py               # env + pricing
│   ├── prepare_data.py         # one-command build
│   ├── normalize.py
│   ├── policy.py
│   ├── replies.py
│   ├── extract.py
│   └── store.py
├── eval/
│   ├── cases.json
│   ├── run_eval.py
│   └── results/<ts>_before.json, <ts>_after.json
├── tests/
│   ├── test_normalize.py
│   ├── test_policy.py
│   ├── test_api_contract.py
│   └── test_idempotency.py
├── build/                      # generated, gitignored (or committed manifest only)
├── Dockerfile
├── render.yaml
├── .env.example
├── .gitignore
└── requirements.txt
```

---

## 4. Test gates (do not skip)

| Gate | Check | Blocking? |
|---|---|---|
| G1 | `python -m src.prepare_data` twice → identical output hashes | yes |
| G2 | Schema validation of all 10 public tickets | yes |
| G3 | 15 labelled tickets: outcome/flag/role/amount match | yes |
| G4 | Safety negatives: injection, mismatch, >$150, out-of-window, unavailable carrier, duplicate | yes (100 %) |
| G5 | Double decision → one ledger row | yes |
| G6 | Same ticket posted twice → same run, one ledger row | yes |
| G7 | Every response has model id + token counts + cost | yes |
| G8 | Deployed URL health + one end-to-end PEND approval | yes |
| G9 | No secrets committed | yes |

---

## 5. Risks and mitigations

| Risk | Mitigation |
|---|---|
| FAQ conflicts with RP-3.2 | Explicit precedence check + documented one-liner; never read FAQ values |
| Dirty dates/ids/money | Normalise centrally; unit tests with every observed variant |
| LLM hallucinates amount/outcome | LLM never decides money; deterministic engine + numeric guard in reply |
| Injection succeeds | Regex + model flag; deterministic escalate before any refund branch |
| Prior refund double-pay | Subtract `ISSUED` + approved local proposals per line |
| Ambiguous multi-order email | Return `UNABLE_TO_VERIFY`, ask for one order; no guess |
| Free-host sleep/restart | Startup data build; SQLite persisted on instance; document ephemeral limit |
| Time overrun | Cut optional `/metrics` and LLM prose first; never cut eval or safety cases |

---

## 6. Definition of done

- Public service URL returns valid runs for unseen emails, including one waiting for approval.
- All five endpoints work; decisions are idempotent and ledger is append-once.
- `ARCHITECTURE.md`, `PLAN.md`, `APPROACH.md` (≤250 words), `ARTEFACT.md`, `README.md` complete.
- Two timestamped eval result files committed, with the change and delta in `ARTEFACT.md`.
- Data build is one command, reproducible, and `data/` is byte-identical to the provided export.
- No secret in git; deployed service uses its own model key.

# Approach

**Findings.** The export is messy: 49 order rows collapse to 47 orders; references
arrive as `LF-100200`, `LF100201`, `#100202` or `order 100203`; 13 dates are `DD/MM`; money is
`$129.00`, `USD 24.00` or plain; three shipments are `UNAVAILABLE`; four scans predate shipment;
three lines already carry refunds. The decisive conflict is `help_centre_faq_v2` (14-day window,
$250 limit, 5-day tracking) versus `refund_policy_RP-3.2`, effective 2026-07-01 and
replacing earlier guidance. I use **RP-3.2 only**: 30 days, 45 with a photo for damage, **$150**
approval threshold.

**Design.** `POST /tickets` runs a pattern router (`src/guardrails.py`) extracting intent, order
reference, damage/wrong-item signals and injection attempts without a model call. Only
low-confidence mail reaches the optional LLM classifier (`src/extract.py`). A deterministic engine
(`src/policy.py`) applies checks, amounts, windows and authority; the LLM never computes money. A
deterministic sanity gate fails closed; an optional LLM judge can only block. Templates
(`src/replies.py`) prevent amount drift. SQLite (`src/store.py`) enforces one run per ticket, one
decision per run and one ledger row per action.

**Evaluation.** `eval/run_eval.py` runs 40 cases: 15 senior-agent labels, 10 public tickets and 15
new negatives/boundaries (injection, identity mismatch, missing order, photo/window edges, carrier
gaps, duplicate refunds, formatting variants). Pass requires exact outcome, approval flag, approver
role, amount within $0.01 and reason-code set. Before the 45-day photo exception: **37/40 (92.5%)**;
after: **40/40 (100%)**; results in `eval/results/`. Contract tests cover decision/ledger
idempotency.

**Judgement calls.** The 10-day test is strict `>`; ambiguous facts return `UNABLE_TO_VERIFY`;
approved proposals count as prior credits.

**Not finished.** Deployment and real-provider cost/latency need the service's own key.

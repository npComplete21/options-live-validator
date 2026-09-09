# options-live-validator — Implementation Plan (v2)

Status: **design for approval — no code written yet**
Date: 2026-09-08
Supersedes v1 (2026-09-07), which was written against a stale backtest-lab
draft and a weekly-expiry assumption. See §0.

Program context: [[project-options-program]]. Theory throughout is from
[[reference-natenberg-option-volatility-pricing]].

---

## 0. What changed from v1, and the one structural consequence

v1 was written before three things were known: backtest-lab dropped Spark for
Polars/DuckDB and shipped its pricing module, its strategy DSL landed, and
the target here became **QQQ 0DTE with a daily strategy tournament**.

The structural consequence is the important one.

backtest-lab prices synthetic chains from `^VXN` (30-day), with term shape
borrowed from the SPX complex whose shortest anchor is `^VIX9D` — nine days.
Reaching τ ≈ 0 from there is not interpolation, it is invention, and 0DTE is
precisely where the term structure is most violently non-linear and where
implied must collapse into realised intraday.

**backtest-lab cannot backtest 0DTE today, and no parameter sweep fixes it.**

So the relationship v1 described is inverted. For this program the live
validator is not validating a backtest — **it is the primary data source.**
Recording the QQQ 0DTE chain intraday builds the only dataset from which a
0DTE backtest can later be run by replay. Every session not recorded is a
session permanently absent from that dataset, which is the strongest argument
in this document for building Phase 1 first and quickly.

---

## 1. The return target as an engineering constraint

Starting equity **$250,000**. Target **12–15%** over 252 sessions:

| | annual | per session | compounded |
|---|---|---|---|
| 12% | $30,000 | ~$119/day | 4.5 bps/day |
| 15% | $37,500 | ~$149/day | 5.5 bps/day |

The daily average is undemanding. The distribution is the entire problem.

Let `p` = win rate, `w` = average winning day, `L` = average losing day.
Hitting the target requires `p·w − (1−p)·L ≥ target`, so:

```
L_max = (p·w − target) / (1 − p)
```

Worked at plausible 0DTE numbers — `p = 0.85`, `w = $750` (0.3% of equity),
target `$137/day`:

```
L_max = (0.85 × 750 − 137) / 0.15 = $3,337
```

**A losing day cannot average more than ~4.4× a winning day.** Break-even
ratios before any profit at all: 4:1 at `p = 0.80`, 5.7:1 at `p = 0.85`,
9:1 at `p = 0.90`. Every one of those sits inside the range a single
unhedged 0DTE gap can produce.

### Why this is also the fast kill criterion

v1 §1 argued that paper P&L takes years to become significant, and that
remains true. But `p` and `L/w` are estimable from roughly 40–60 sessions,
and together they are a **sufficient statistic for whether the target is
reachable at all.** If a strategy's measured `L/w` exceeds `p/(1−p)`, it
cannot reach 12–15% no matter how long it runs, and that verdict is
defensible in a quarter rather than a decade.

**Every strategy in the tournament is scored against its own `L_max` ceiling
from day one.** That is the primary metric, not cumulative P&L.

---

## 2. What Natenberg says about this specific trade

Recorded here because it is a standing constraint, not a one-time caveat.
The book is unusually direct about short-dated short gamma:

- *"Short-dated ATM options in a quiet market are the most explosive
  instruments in the book. **Never be short size in them near expiry.**"*
  ATM gamma rises into expiry **and** as vol falls — both at once.
- *"Near expiry the model stops being trustworthy. Traders who keep obeying
  it there get carried out."*
- Gap damage scales with gamma and so concentrates in short-dated ATM options
  in low-vol regimes. The stated corollary is that **buying** cheap
  near-expiry straddles is positive-expectancy, because the model prices no
  gap risk: *"You lose small, often. That is the trade."*

A 0DTE short strangle systematically sells what the book identifies as
structurally underpriced-by-the-model. This does not make it unworkable — the
intraday 0DTE IV/RV premium is real and win rates are genuinely high — but it
fixes what we are building: **short gamma at the point of maximum gamma,
where the risk controls are not a feature of the strategy, they are the
strategy.**

It is also why the tournament (§5) includes a long ATM straddle. That is the
book's own claim as a null hypothesis, it costs almost nothing to run in
paper, and if it beats the short-premium book on QQQ 0DTE it is the single
most valuable finding available here.

### Rules encoded as engine constraints, not documentation

| Natenberg rule | Engine constraint |
|---|---|
| Never adjust by adding to a losing structure | Management actions are restricted to `close`, `close_partial`, `hedge_underlying`. Anything increasing short quantity in an open position is **rejected at config load**, and per-strategy whitelisting requires an explicit loud flag. |
| Adjust in the underlying to change only delta | `hedge_underlying` trades QQQ shares, never options. Option-leg adjustment changes gamma/vega too and is a different decision. |
| Size and risk do not correlate in options | Equal-**risk** sizing across the tournament (§6), never equal contract count. |
| Theoretical edge is not the objective — margin for error is | Sizing is driven by a max-loss budget, not by premium collected. |
| Watch the net contract position | Logged daily per run alongside the greeks. Greeks are local; that number is global. |
| Companion calls and puts have identical gamma and vega | The greek/residual layer keys volatility exposure by **(strike, expiry)**, never by call-vs-put. |

---

## 3. The 0DTE modelling problem: which clock measures τ

**This is the decision that most changes the output, and it is easy to miss.**

At 09:45 on expiration day there are 6.25 hours to the close. Expressed as a
year fraction:

| clock | τ | σ√τ relative |
|---|---|---|
| calendar (6.25 / 24 / 365) | 0.000713 | 1.00× |
| trading hours (6.25 / (6.5 × 252)) | 0.003815 | **2.31×** |

The two conventions differ by a factor of 5.3 in τ and therefore **2.3× in
σ√τ**. Since every strike selector is expressed in deltas or standard
deviations, the clock choice changes *which strikes the strategy sells* by
better than a factor of two. It is not a rounding detail; it is the largest
single modelling lever in the repo.

Worse, intraday volatility is not uniform — it is U-shaped, heavy at the open
and into the close — so even a trading-hours clock misstates τ through the
session.

**Recommendation:** trading-hours clock for v1, with the intraday
vol-weighting curve **measured from our own recorded data** (Phase 2) and
substituted once it exists. This is a genuine chicken-and-egg: the right
clock is only knowable after recording, which is another reason recording
leads the build order.

**Hard requirement:** whatever clock is chosen must be identical in
backtest-lab and here, or no live-vs-backtest comparison means anything. It
belongs in the shared package, not in either repo's config.

### Delta instability near the close

As τ → 0 delta approaches a step function, so a "10-delta strike" at 15:30 is
almost at the money. Consequences, both design decisions:

- **Strike selection happens at entry only.** Never re-select by delta
  intraday.
- **Management triggers must not be delta-based on the option legs.** Use
  underlying touch of the short strike, premium multiple, or portfolio delta
  for hedging. Per-leg delta thresholds are meaningless in the last hour.

---

## 4. Strategy definition — reuse backtest-lab's DSL, extend for intraday

backtest-lab already defines legs as `(right, expiry selector, strike
selector, quantity)` with `delta` / `stddev` / `atm` / `offset` selectors, an
`offset` dependency DAG, Pydantic-generated parameter schemas, and a
`Strategy` ABC escape hatch. **That abstraction is correct and 0DTE falls out
of it for free** — `expiry: {dte: {target: 0, tolerance: 0}}`.

Forking it would be the single worst decision available: two DSLs means the
live and backtested strategies are only *claimed* to be the same. What this
repo needs is a small set of intraday extensions, **contributed back to
backtest-lab so one vocabulary serves both**:

| Addition | Purpose |
|---|---|
| `entry.trigger.time_of_day` | intraday entry (`"09:45"`, tz-aware) |
| `management.time_of_day` + `action: force_flat` | the mandatory pre-close flatten |
| `action: hedge_underlying` | delta adjustment in shares (Natenberg rule 5) |
| `management.abs_portfolio_delta` | hedge trigger that survives τ → 0 |
| `management.underlying_touch: {leg: <id>}` | stop that is meaningful at 0DTE |

Example — the v1 baseline, in the existing vocabulary:

```yaml
name: short_strangle_0dte
legs:
  - {id: short_put,  right: P, qty: -1, expiry: {dte: {target: 0, tolerance: 0}},
                                        strike: {delta: "{{short_put_delta}}"}}
  - {id: short_call, right: C, qty: -1, expiry: {same_as: short_put},
                                        strike: {delta: "{{short_call_delta}}"}}
params:
  short_put_delta:  {type: float, default: 0.10, min: 0.01, max: 0.50}
  short_call_delta: {type: float, default: 0.10, min: 0.01, max: 0.50}
entry:
  trigger: {time_of_day: "09:45", tz: America/New_York}
  filters: [{no_open_position: true}]
management:
  - {profit_target: {pct_of_credit: 0.50},     action: close}
  - {underlying_touch: {leg: short_put},       action: close}
  - {underlying_touch: {leg: short_call},      action: close}
  - {time_of_day: "15:45",                     action: force_flat}
```

---

## 5. The v1 tournament — five strategies

Mapped to Natenberg's four quadrants, deliberately spanning risk profiles
rather than clustering in one.

| # | Strategy | Quadrant | Role in the tournament |
|---|---|---|---|
| 1 | `short_strangle_0dte` — 10Δ both sides | −γ −ν | baseline; max edge, undefined risk |
| 2 | `iron_condor_0dte` — 10Δ shorts, wings offset | −γ −ν | defined risk; smaller edge, sizable up (rule 2) |
| 3 | `iron_butterfly_0dte` — ATM shorts, wings offset | −γ −ν | max premium, **max gamma — the riskiest member**, flagged per the "never be short size in short-dated ATM" rule |
| 4 | `short_strangle_0dte_hedged` — #1 + `hedge_underlying` at portfolio delta breach | −ν, γ managed | does hedging rescue the naked strangle, and at what cost in adjustment slippage? |
| 5 | `long_straddle_0dte` — long ATM | +γ +ν | the Natenberg contrarian; the null hypothesis for the whole book |

All five are YAML over the shared DSL. #2 and #3 differ from #1 only by two
`offset` wing legs — which is the acceptance test that the abstraction is
real, and it mirrors backtest-lab's own Phase 6 gate: **add a strategy on a
second instrument with zero engine changes.**

---

## 6. Equal-risk sizing — the only fair tournament

Five strategies with different margin profiles cannot be compared at equal
contract count: the naked strangle would simply look best because it is the
most levered. Per Natenberg rule 2 — *size and risk do not correlate; small-
risk structures can be done in size that equalises total edge* — each
strategy is sized so its **modelled max loss equals a common daily risk
budget `R`**.

- **Defined risk (#2, #3):** max loss is exact — `wing_width × 100 − credit`.
  Contracts = `floor(R / max_loss_per_unit)`.
- **Undefined risk (#1, #4, #5):** no true max loss exists. Size against the
  **stop level** as a risk proxy, and record explicitly that this is a *soft*
  cap a gap can breach.

**That asymmetry is itself a headline finding, not a footnote.** The defined-
risk book's max loss is a fact; the naked book's is a hope. The reports must
never present the two as the same quantity (§10), which is the same discipline
as the `confidence: directional_only` marker in
[[project-backtest-vol-model-confidence]].

`R` starts at **0.5% of equity = $1,250/day/strategy**, configurable. It is
deliberately *not* derived from the §1 ceiling, because `p` and `w` are
unknown until measured — measuring them and then solving for whether 12–15%
is reachable is the experiment.

**Each strategy gets its own notional $250k book.** They are independent
comparable accounts, so one blowing up does not distort the others' returns
or margin. Configurable to a shared book later.

---

## 7. Capital and margin

Matching backtest-lab §8, so the two agree:

- Reg-T-style margin recomputed continuously, not just at entry
- **per-strategy margin rules** — defined-risk structures margin at wing
  width, naked shorts at the Reg-T short-option formula
- max-utilisation cap driving position size
- 0DTE margin has an intraday character worth modelling: as a short strangle
  goes ITM late in the session, margin expands exactly when the position is
  losing. That interaction is a real source of forced closes and must not be
  assumed away

---

## 8. The daily lifecycle

```
pre-open   load configs; assert MODE=PAPER; resolve today's 0DTE expiry;
           build the moneyness-band watch set; verify it is a trading day
09:30      begin recording the chain (§9) — recording is independent of
           and outlives every strategy decision
09:45      entry trigger: each strategy resolves strikes from live-derived
           IV, sizes to R, submits paper orders
intraday   manage per config — profit target, underlying touch, portfolio
           delta hedge; continuous margin and greek marking
15:45      force flat, all strategies, unconditionally
16:00      settle; write daily.parquet, residuals, tournament scorecard
```

Positions never survive the session. This materially simplifies resumability
(§11): a mid-day restart only has to recover *today*.

---

## 9. Feed and recording

**Source:** broker paper feed first (Alpaca paper / Tradier sandbox), on a
trial, evaluated against: real OPRA bid/ask **with sizes**; snapshot cadence
and rate limits; multi-leg 0DTE order support; documented paper fill engine.
If the paper tier turns out to serve indicative or delayed quotes, it cannot
measure spreads and the paid feed decision reopens immediately (§14.4).

**Greeks and IV are computed here**, from the real mid via backtest-lab's
pricer — never taken from the vendor, whose IV embeds a vendor's rate,
dividend and *τ clock* assumptions (§3) and would silently contaminate both
strike selection and every residual.

**Watch set:** today's 0DTE expiry, moneyness band `|k| ≤ 0.05` (0DTE needs
a far narrower band than the 0.15 in v1 — nothing beyond it has meaningful
premium), both rights. Roughly 100–200 contracts.

**Cadence:** 1s for strikes with an open position, 5s for the rest of the
band. Session volume ≈ 200 contracts × ~2,000 samples ≈ 400k rows/day —
trivially small, which is worth stating explicitly before anyone sizes
infrastructure for it (§14.1).

**Hygiene gate unchanged from v1 §5.3** — crossed/locked, zero bid, stale
timestamp, absurd width, underlying/option timestamp skew. Rejections are
counted and published, never silently dropped: at 0DTE the wings go
untradeable for stretches of the session, and **how often the strategy simply
could not have traded is a first-class result.**

---

## 10. Fills — likely the dominant term, not the vol premium

Conservative model, unchanged in spirit from v1 §7: sell at bid, buy at ask,
no price improvement, size checked against displayed size. The broker's own
paper fills are recorded as a shadow series so its optimism is measurable.

**But 0DTE changes the magnitude of this term.** A 10-delta QQQ 0DTE option
may be $0.05 bid / $0.09 ask. An iron condor crosses four legs on entry and
four on exit; a strangle crosses four in total. On a credit measured in tens
of cents, **the spread is plausibly larger than the entire volatility
premium.**

So the honest prior is that transaction costs, not the IV/RV edge, decide
whether any of this reaches 12–15%. Reporting must decompose P&L into
`vol premium − spread paid − commission − slippage` so that conclusion is
visible rather than inferred. If the answer is "the edge is real but the
spread eats it", that is a *useful* result and it points at execution
(mid-price limit orders, legging) rather than at strategy selection.

---

## 11. Kafka topology — and the subtlety that decides it

Kafka stays because **operating it is a goal of the project**, not because the
throughput demands it (§14.1). That makes the design question sharper rather
than softer: a single-partition topic with one consumer would technically
satisfy `CLAUDE.md` while teaching nothing. The topology below is chosen to
exercise the parts that actually matter — keys and partitions, consumer
groups, manual offset control, retention versus archival, and replay.

### The ordering problem that shapes everything

The obvious design is one topic, `quotes.QQQ`, keyed by OCC contract symbol.
Keying by contract is correct for what it guarantees: every quote for a given
contract lands in one partition, so per-contract ordering holds.

But Kafka orders messages **only within a partition**. Spread ~200 contracts
across partitions and a consumer sees them interleaved with no global order —
while a strategy decision needs a *coherent snapshot of the whole chain at one
instant*, not a stream of unordered per-contract updates. Reconstructing
snapshots downstream means event-time windowing with a watermark and a
late-arrival policy: real streaming work, and a rich source of subtle bugs.

So: two topics, each keyed for what it must guarantee.

| Topic | Key | Partitions | Consumed by | Guarantee |
|---|---|---|---|---|
| `quotes.QQQ` | OCC contract symbol | 6 | archiver, surface/residual jobs | per-contract ordering; parallel consumption |
| `chain.QQQ` | ticker (single key) | 1 | the five strategy consumers | **total ordering of whole-chain snapshots** |

One `chain.QQQ` message is one complete band snapshot at one instant — ~200
contracts, ~20–30 KB, comfortably inside the 1 MB default. A single partition
costs nothing at this volume and buys strict global ordering, which is exactly
the guarantee a trading decision requires.

This is the fine-grained-events + materialised-snapshot pattern, and carrying
both makes the tradeoff concrete rather than theoretical: the same data, keyed
two ways, with different ordering guarantees and different consumers.

### Consumer groups are the tournament

Each strategy is its own consumer group on `chain.QQQ`. All five see every
snapshot independently; one falling behind or crashing does not affect the
others; each keeps its own offsets. That is precisely what consumer groups
are for, and it is why the tournament is a genuine fit for Kafka rather than
a contrivance built to justify it.

### Offsets — the one lesson worth the whole exercise

`enable.auto.commit=false`, always. Auto-commit is the most common source of
correctness bugs in Kafka applications, and here it breaks the system
silently: consume a snapshot → open a position → crash before the commit →
the snapshot is redelivered → **the position opens twice.**

The fix, per §12: the consumed offset is written **inside the same DynamoDB
transaction** as the state mutation it caused, and on startup the consumer
`seek()`s to the stored offset instead of trusting the broker's committed
one. Kafka's own commit becomes a monitoring signal, not the source of truth.

### Retention versus archival

`quotes.*` and `chain.*` retain 7 days — enough to replay a week of sessions
straight from the log. Permanent history is the archiver's Parquet (§12),
because **Kafka is a buffer, not a database.** Building that split
deliberately is worthwhile: it is the distinction most often gotten wrong.

Replay from a Parquet recording and replay from a Kafka offset must drive the
**same** engine, so a strategy change can be re-run either way and produce
identical results.

### Local development

Single-broker **KRaft** (no ZooKeeper — removed in Kafka 4.x) via docker
compose, mirroring the single-broker EC2 target in `CLAUDE.md`. MSK Serverless
only once the setup is proven, also per `CLAUDE.md`.

### Client

`confluent-kafka` (librdkafka) rather than the `kafka-python` currently in
`requirements.txt` — it is the industry-standard client, actively maintained,
and the one worth learning. Reverting is a one-line change if you would rather
read pure Python.

---
## 12. State, archive, and resumability

DynamoDB as the operational store, single table keyed by run, per v1 §8 —
with the **transactional offset-with-state** rule retained: the consumed feed
position is written in the same transaction as the state mutation it caused,
so an at-least-once redelivery cannot double-open a position. Deterministic
idempotency keys on every order as the second layer.

Intraday-only positions shrink the blast radius: recovery scope is one
session, and a restart after 15:45 has nothing to recover.

S3 archive per v1 §9, with one addition that is now the most important
prefix in the repo:

```
s3://<bucket>/live/chains/QQQ/date=<YYYY-MM-DD>/    # the 0DTE recording — the dataset
```

This is what backtest-lab eventually replays. It should be written in a schema
that matches its processed-chain zone column-for-column wherever possible, so
replay needs no translation layer.

---

## 13. Reporting

**A. Tournament scorecard (daily, primary).** Per strategy: win rate `p`,
average win `w`, average loss `L`, **realised `L/w` against the `L_max`
ceiling from §1**, worst day, net contract position, margin peak, and
cumulative P&L against the 12–15% band. The ceiling comparison is the metric
that can retire a strategy in a quarter.

**B. Cost decomposition (daily).** `vol premium − spread − commission −
slippage` per §10.

**C. Surface and assumption report.** `sigma_market` by strike and time of
day; the fitted intraday vol curve feeding the τ clock (§3); observed
half-spread in vol points; quote rejection rates. This is the output
backtest-lab consumes, and per §0 it is the reason this repo exists first.

Every P&L view carries trade count and the §1 caveat. Defined-risk and
undefined-risk max losses are never rendered in the same column without a
marker (§6).

---

## 14. Open decisions

1. **Kafka — RESOLVED 2026-09-08: a learning requirement, so it stays.**
   §9's volume (~400k rows/day) does not need it and deterministic replay is
   available from Parquet directly, so this is explicitly a learning goal
   rather than a throughput one — recorded here so nobody later "optimises"
   it away as dead weight. Topology in §11 is therefore designed to exercise
   partitioning, consumer groups and manual offset control rather than to be
   minimal. Kafka now enters at Phase 1, not Phase 5.
2. **Snapshot cadence** — 1s/5s as proposed, or finer? Finer costs nothing in
   storage and cannot be recovered retroactively.
3. **Entry time** — fixed 09:45, or swept across the session? Entry timing is
   plausibly the highest-sensitivity parameter in a 0DTE strategy, and the
   recorder makes sweeping it free after the fact.
4. **Paid data** — revisit immediately if the broker paper tier serves
   indicative quotes, and separately for the *historical* 0DTE chains that
   would let backtest-lab replay years instead of starting the clock today.

---

## 15. Build order

| Phase | Deliverable | Gate |
|---|---|---|
| 0 | config, mode guard (v1 §4), capital model, τ clock, DSL loader reusing backtest-lab's, **local KRaft Kafka + topic provisioning** | guard tests pass incl. every mis-configuration case; broker up, topics created with the §11 keys and partition counts |
| 1 | **the recorder** — feed → `quotes.QQQ` + `chain.QQQ` producer, hygiene gate, archiver consumer → Parquet. No orders, no strategies | a full clean session on disk *through Kafka*; rejection rates sane; archiver survives a broker restart |
| 2 | IV/greeks via the shared pricer; τ-clock calibration; intraday vol curve; report C | reproduces vendor greeks to a stated tolerance; clock chosen on measured data |
| 3 | **offline tournament** — replay recordings through all five strategies with conservative fills | five strategies, zero engine changes between them; one day hand-checked |
| 4 | live paper loop (same engine, live feed), state store, restart test | kill mid-session, restart, no lost or duplicated position |
| 5 | broker paper order submission | broker fills alongside ours; gap quantified |
| 6 | sweeps (entry time, deltas, wing widths, stops), reports A and B | scorecard answering §1's reachability question |

**Phase 3 is where the tournament starts producing results** — on recorded
data, with no broker account and no waiting for live sessions. The same engine
serves replay and live, which is the deterministic-replay property that makes
strategy changes testable without spending another quarter of real time.

Phases 0–3 need no DynamoDB, no broker credentials and no paid data — Kafka
runs locally in docker throughout. Phase 1 should start immediately regardless
of every other open question, because the dataset only accumulates forward
(§0).

---

## 16. Dependencies

- `options-backtest-lab @ git+https://github.com/npComplete21/options-backtest-lab@<tag>`
  — the pricer and the strategy DSL. Its `pyproject.toml` is already published
  and its runtime deps (numpy/scipy/polars/pydantic/pyyaml) carry no Spark, so
  it imports cleanly into a streaming process.
- `polars`, `duckdb`, `pyarrow` — matching backtest-lab's stack, not v1's
  Spark/Athena
- `pandas_market_calendars` — pinned to backtest-lab's version so expiries and
  session boundaries are identical by construction
- broker SDK — pending §13.4
- `pytest`, `pytest-cov`, `ruff`
- `confluent-kafka` — replaces the `kafka-python` in `requirements.txt` (§11)
- Kafka itself runs locally via docker compose in KRaft mode; no Java or
  ZooKeeper install needed on the host

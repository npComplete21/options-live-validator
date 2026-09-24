# options-live-validator — Project Status

_Last updated: 2026-09-24_

A **living snapshot**, fully overwritten on each update — not a history log.
Design reasoning lives in [`IMPLEMENTATION_PLAN.md`](IMPLEMENTATION_PLAN.md);
this file says where things stand and what to do next.

---

## Where we are

Phases 0, 1 and the computable half of Phase 2 are built and verified. The
pipeline runs end to end: synthetic feed → hygiene gate → two Kafka topics →
Parquet → implied vol and greeks → report C.

**Nothing has been recorded from a real market.** That is now the single
binding constraint: it blocks Phase 2's gate, the τ-clock calibration, and —
per plan §0 — every session that passes unrecorded is permanently absent from
the dataset this whole program depends on.

---

## Build order progress

Phases as defined in [`IMPLEMENTATION_PLAN.md`](IMPLEMENTATION_PLAN.md) §15.

- [x] **Phase 0** — config, mode guard, τ clock, Kafka topology, local broker
      (PR #1)
- [x] **Phase 1** — the recorder: watch set, hygiene gate, producer, Parquet
      archiver (PR #2)
- [x] **Cross-repo** — shared τ clock adopted; `options-backtest-lab` merged,
      packaged as `obl`, tagged `v0.2.0` (PR #3)
- [~] **Phase 2** — IV/greeks **done**; report C **done**; τ-clock calibration
      and the intraday vol curve **blocked on real data**; the phase gate is
      **undischarged** (see below)
- [ ] **Phase 3** — offline tournament: replay recordings through five
      strategies with conservative fills
- [ ] **Phase 4** — live paper loop, DynamoDB state, restart test
- [ ] **Phase 5** — broker paper order submission
- [ ] **Phase 6** — sweeps, reports A and B

### Phase 2's gate is not met, and cannot be yet

§15 states it as *"reproduces vendor greeks to a stated tolerance; clock chosen
on measured data"*. Both halves need data that does not exist:

- **No vendor.** No broker account, and `olv.feed.client.QuoteFeed` deliberately
  excludes greeks from the feed interface, so there are no vendor greeks to
  compare against.
- **No measured session.** Every row recorded so far is synthetic.

What stands in its place is a `price → IV → price` round-trip against a pricer
that `options-backtest-lab` cross-validates with QuantLib. That is an
independent reference, but it is **not the same claim** and is recorded as such
in `tests/test_surface.py`'s module docstring rather than allowed to read as
the gate.

The intraday vol curve is unbuilt for the same reason, and
`obl.timebase.VolWeightedClock` still raises. Fitting it to the synthetic feed
would calibrate τ — the largest modelling lever in the repo — against output
that feed's own docstring forbids using in analysis.

---

## Verified state (checked 2026-09-15, from a clean venv)

- Clean venv + `pip install -e '.[dev]'` resolves `options-backtest-lab 0.2.0`
  from the pinned git tag
- **190 tests passing** (178 unit + 12 integration against a live broker);
  `ruff check` and `ruff format --check` clean
- Recorder end to end: 8 snapshots, 740 quotes, 60 rejected (7.5%), reasons
  broken out
- Report C renders from a real Parquet archive via `python -m olv.surface`
- The shared clock reproduces §3's figures from a **dependency-free** install:
  calendar τ `0.000713`, trading-hours τ `0.003816`, σ√τ ratio **2.3126**

~2,440 lines of source, ~1,810 lines of tests.

---

## What's done, and why it was built that way

### Phase 0 — `src/olv/common/`

- **`mode.py`** — execution mode is an **enum, never a bool**, and
  `resolve_endpoint()` is the only thing permitted to construct a broker base
  URL, so the host cannot be set independently of the mode. No production
  hostname is committed; a test greps `src/` to keep that true.
- **`sessions.py`** — exchange sessions with real open/close instants, so early
  closes shorten τ correctly. A half-day 0DTE strategy has barely half its
  usual time to expiry; assuming 16:00 would overstate τ ~2x on exactly the
  days liquidity is worst. Now also this repo's implementation of the shared
  clock's `SessionSource` protocol.
- **`kafka.py`** + `docker-compose.yml` — two topics keyed for what each must
  guarantee: `quotes.QQQ` (6 partitions, per-contract ordering) and `chain.QQQ`
  (1 partition, **total ordering** of whole-chain snapshots, because a trading
  decision needs a coherent instant, not interleaved per-contract updates).
  Topic auto-creation disabled so partition counts stay a design decision.
- **`ids.py`, `models.py`** — run identifiers, row schemas, OCC symbols.

### Phase 1 — `src/olv/feed/`, `src/olv/state/archive.py`

- **`client.py`** — the `QuoteFeed` protocol is the vendor boundary; everything
  downstream is vendor-independent, so a broker drops in as one class. Greeks
  and IV are **excluded on purpose**: a vendor's IV embeds that vendor's clock,
  rate and dividend conventions, and the clock alone moves σ√τ by 2.31x at 0DTE.
- **`universe.py`** — the watch set is rebuilt **every snapshot**, because spot
  moves and a band pinned at the open drifts off centre by the afternoon,
  quietly dropping the strikes that matter.
- **`gate.py`** — quote hygiene. Rejections are **counted and published, never
  silently dropped**: at 0DTE the wings go untradeable for stretches, so how
  often a strategy *could not have traded* is a first-class result.
- **`synthetic.py`** — a fixture generator, explicitly **not** a pricing model.
  Every row is stamped `feed_source`, so fabricated data is self-identifying in
  storage forever.
- **`archive.py`** — Kafka → Parquet. Offsets commit **only after** a durable
  write: at-least-once by choice, since a duplicate row dedupes on
  `(symbol, quote_ts)` while a lost row cannot be re-observed.

### Cross-repo — the shared τ clock

The clock had **forked**: both repos wrote one independently, each describing
itself as the extraction point for the shared package. They turned out to be
complementary rather than rival — backtest-lab's was date-resolution and
explicitly *refused* 0DTE; this repo's counted real session seconds and was the
implementation that refusal was waiting for.

Merged into **`obl.timebase`**: timezone-aware instants throughout, sessions
injected so the module stays **pure-stdlib** (a streaming process can depend on
it without pulling `pandas_market_calendars`, numpy or polars), backtest-lab's
`id`/registry kept for `run_id` reproducibility. This repo's
`src/olv/common/clock.py` is **deleted**; `tests/test_shared_clock.py` binds the
shared clock to our real exchange calendar.

Along the way, `options-backtest-lab` turned out **not to be importable at
all** — `timebase.py` was a top-level module under `src/` and never reached the
wheel, the other packages were flattened to top-level names while the code
imported `src.X`, and neither registry's YAML shipped. All three were invisible
from inside a checkout because pytest puts the repo root on `sys.path`. It is
now packaged as `obl` and tagged `v0.2.0`.

### Phase 2 — `src/olv/analytics/`, `src/olv/reporting/surface.py`

- **`forward.py`** — the forward is **measured, not assumed**. The usual
  `F = S·exp((r−q)τ)` imports a rate and a dividend into every recorded IV, and
  at 0DTE gets the forward wrong by ~7.6¢ on a $500 underlying — about a tick,
  and a large fraction of a wing quoted $0.05 bid. Put-call parity gives it
  directly from quotes we already have. The estimator is a **median** across the
  nearest strikes: a stale print is routine at 0DTE, and the forward feeds every
  σ in the snapshot, so it is the wrong place to average in an outlier. `F − S`
  comes back as a result of its own — the market's rate, dividend and borrow
  observed rather than assumed.
- **`surface.py`** — σ at bid/mid/ask plus greeks, as a **pure function**, so
  §11's requirement that Parquet replay and Kafka replay drive the same engine
  holds by construction: there is one engine and the drivers sit outside it.
  σ at bid *and* ask because §10's hypothesis is that the spread, not the IV/RV
  edge, decides the outcome — and a mid-only σ cannot show that. Rows whose vol
  is not recoverable carry a status rather than being dropped.
- **`reporting/surface.py`** — report C, which names what it **cannot** show
  (see Known gaps) rather than rendering three quarters of itself as complete.
  A synthetic recording is labelled on the report's face, and one fabricated row
  disqualifies the whole report.
- **`olv/surface.py`** — `python -m olv.surface --archive … --ticker QQQ`.

---

## Known gaps (found during Phase 2, not yet fixed)

1. ~~Rejections never reach Parquet.~~ **Fixed 2026-09-24.**
   `RejectionArchiver` consumes `chain.<ticker>` into its own Hive dataset
   beside the accepted quotes, and report C now carries rejection rates by
   reason and per snapshot.
2. **No real recording.** See "Immediate next action".
3. **`VolWeightedClock` unimplemented** — blocked on (2).
4. **The enriched zone is not persisted.** `olv.surface` recomputes σ and greeks
   on every run. Deliberate while the clock was settling; worth writing before
   Phase 3 replays the archive repeatedly, with the clock `id` on every row.

---

## Not yet built

- `src/olv/strategy/`, `src/olv/broker/` are empty — no strategies, no fill
  model, no order submission.
- No vendor feed client. Deliberately last: everything downstream of
  `QuoteFeed` is vendor-independent, so a broker drops in as one class.
- The archiver writes to a local `Path`, **not S3**.
- No DynamoDB, no transactional offset-with-state, no broker credentials.
- Nothing imports backtest-lab's **pricer** in `src/` yet — only the shared
  clock and, in the analytics layer, `black_scholes`.

---

## Immediate next action

**Choose a market-data source and start recording.** Everything else is
downstream of it, and §0 makes the cost of delay compound daily.

### Broker research (2026-09-15) — free real-time OPRA is the constraint

Free *paper trading* is easy; free *real-time OPRA with sizes* is the hard part,
and §9's criterion is the latter.

| Option | Paper endpoint | Free-tier options data | Verdict |
|---|---|---|---|
| Alpha Vantage (MCP already configured) | n/a | **premium-gated**; realtime returns placeholder rows | fails |
| Alpaca | real paper endpoint | **indicative only**; OPRA ≈ $99/mo | fails §9 as written |
| Tradier | sandbox | **15-min delayed**; real-time needs a funded account | sandbox fails |
| Schwab Trader API | **none** | **free real-time quotes + option chains** with a brokerage account | passes on data |

**The wrinkle:** the two free real-time paths (Schwab, Tradier production) both
mean **live brokerage credentials**. Schwab has no paper environment at all.
`CLAUDE.md` requires flagging any code path touching a production key, and
`mode.py` assumes a paper host exists. Phases 1–4 only *read*, so this is
workable — but it wants a **read-only feed client that cannot reach an order
endpoint**, rather than relying on mode config not to call one.

**Also worth evaluating:** [0DTESPX.com](https://www.0dtespx.com/) reportedly
offers free 0DTE chain snapshots at 1-second resolution with bids, asks and
greeks across 900+ past sessions. If it holds up it would unblock **Phase 3's
offline tournament with no broker at all**, and supply the historical 0DTE data
§14.4 wants for backtest-lab. The catch: **SPX, not QQQ** — an instrument
change, though backtest-lab's framework is explicitly strategy × instrument as
config.

Entitlements change; confirm on each provider's own pricing page before opening
an account — particularly whether Schwab's free real-time covers *option chain*
quotes **with sizes**, since that distinction is what disqualified Alpaca.

### If the data decision needs time

One piece of local work remains ready and independent: the **Phase 4 Kafka
consumer skeleton**, reusing `olv.analytics` unchanged. Persisting the enriched
zone (gap 4) is worth doing once a real feed fixes the schema.

---

## Open decisions / deliberately deferred

- **Kafka — RESOLVED 2026-09-08.** Stays because operating it is a learning
  goal, explicitly *not* because ~400k rows/day needs it.
- **Broker / data source — OPEN, now the critical path.** See above.
- **Snapshot cadence** — 1s with an open position, 5s otherwise. Finer costs
  nothing in storage and cannot be recovered retroactively.
- **Entry time** — fixed 09:45 or swept? Plausibly the highest-sensitivity
  parameter; the recorder makes sweeping it free after the fact. Deferred to
  Phase 6.
- **Paid data** — reopens if the chosen tier serves indicative or delayed
  quotes, and separately for historical 0DTE chains.
- **Instrument** — QQQ 0DTE is the target; a free SPX dataset could change that.
- **Phase 2's gate wording** — currently unmeetable as written. Worth amending
  deliberately rather than leaving a gate that quietly never passes.

---

## Key design decisions worth remembering

- **This repo is the primary data source, not a validator of a backtest.**
  backtest-lab prices synthetic chains from `^VXN` (30-day) anchored no shorter
  than `^VIX9D`; reaching τ≈0 from there is invention, not interpolation. §0.
- **The τ clock is shared code, not this repo's.** It lives in `obl.timebase`,
  pinned by tag. Adding a local clock or computing a year fraction inline
  recreates the fork closed on 2026-09-09. Bumping the pin can move every
  delta-selected strike — a modelling change, not a dependency bump.
- **The τ clock is the largest modelling lever.** Calendar vs trading-hours is
  5.35x in τ and **2.31x in σ√τ**, moving which strikes get sold by better than
  a factor of two. §3.
- **The forward is measured, not assumed.** Put-call parity removes a ~7.6¢
  0DTE bias and yields the market's own basis as an output.
- **Scoring is `L/w` against the `L_max` ceiling, not cumulative P&L.** A losing
  day cannot average more than ~4.4x a winning one at `p = 0.85`; `p` and `L/w`
  are estimable in 40–60 sessions, making this a kill criterion resolvable in a
  quarter. §1.
- **Transaction costs probably decide the outcome, not the vol premium.** On a
  10-delta QQQ 0DTE option quoted $0.05/$0.09 the spread plausibly exceeds the
  entire premium, so P&L must decompose into
  `vol premium − spread − commission − slippage`. §10.
- **Natenberg's rules are engine constraints, not documentation** — e.g. any
  action increasing short quantity in an open position is rejected at config
  load. §2.
- **Strike selection happens at entry only**, and management triggers are never
  per-leg delta: as τ→0 delta becomes a step function, so a "10-delta strike" at
  15:30 is nearly at the money. §3.
- **An expiry in the past returns τ `0.0` rather than raising** — a deliberate
  loosening when the clocks merged, since backtest-lab needs zero for contracts
  rolled off a chain. The guard moved rather than vanished: an expired contract
  reaching a live decision is a *selection* bug, and `clock_ratio` still refuses
  a degenerate denominator.
- **Defined-risk and undefined-risk max losses are never rendered in the same
  column without a marker.** The first is a fact; the second is a hope. §6.

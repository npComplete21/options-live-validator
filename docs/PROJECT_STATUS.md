# options-live-validator — Project Status

_Last updated: 2026-09-09_

This file is a **living snapshot**, fully overwritten on each update — not a
history log. Design reasoning lives in
[`IMPLEMENTATION_PLAN.md`](IMPLEMENTATION_PLAN.md); this file only says where
things stand right now and what to do next.

## Where we are

Phases 0 and 1 are merged and verified running: the 0DTE chain recorder
carries a synthetic feed through the hygiene gate, into both Kafka topics, and
out to partitioned Parquet. Nothing trades yet and nothing has been recorded
from a real session.

The two cross-repo blockers in front of Phase 2 are resolved in code: the tau
clock is now one shared module in `obl.timebase`, and backtest-lab is packaged
so it can actually be imported. **One manual step remains** — merging
backtest-lab's PR #2 and cutting the `v0.2.0` tag this repo pins.

## Build order progress

Phases as defined in [`IMPLEMENTATION_PLAN.md`](IMPLEMENTATION_PLAN.md) §15.

- [x] **Phase 0** — config, mode guard, capital model, tau clock, Kafka topology
      and provisioning, local KRaft broker — merged in
      [PR #1](https://github.com/npComplete21/options-live-validator/pull/1)
- [x] **Phase 1** — the recorder: watch set, hygiene gate, producer into both
      topics, Parquet archiver — merged in
      [PR #2](https://github.com/npComplete21/options-live-validator/pull/2)
- [~] **Phase 2** — IV/greeks via backtest-lab's pricer, tau-clock calibration,
      intraday vol curve, report C — prerequisites done (shared clock, installable
      dependency); no Phase 2 code written yet
- [ ] **Phase 3** — offline tournament: replay recordings through all five
      strategies with conservative fills
- [ ] **Phase 4** — live paper loop, DynamoDB state store, restart test
- [ ] **Phase 5** — broker paper order submission
- [ ] **Phase 6** — sweeps, reports A and B

## Verified state (checked 2026-09-09, not just asserted)

- `make test` → **130 passed**
- `make test-all` → **+12 integration passed** against the running broker
  (`olv-kafka` container healthy)
- End-to-end recorder run:
  `python -m olv.record --snapshots 8 --interval 0 --defect-rate 0.08`
  → 8 snapshots, 740 quotes published, 60 rejected (7.5%), rejection reasons
  broken out by category
- Branch `claude/project-status-next-steps-7131e1` is level with `origin/main`
  (0 ahead / 0 behind); working tree clean

## What's done

**Phase 0 — `src/olv/common/`**

- `mode.py` — execution mode as an **enum, never a bool**, and
  `resolve_endpoint()` is the only thing permitted to construct a broker base
  URL, so the host cannot be set independently of the mode. No production
  hostname is committed; a test greps `src/` to keep that true. Rationale in
  [`CLAUDE.md`](../CLAUDE.md) under Conventions.
- `clock.py` — the tau clock. `VolWeightedClock` is **deliberately absent**:
  intraday vol is U-shaped, but the weighting curve is only knowable from
  recorded data (Phase 2), and guessing it now is exactly the invented-
  assumption problem this program exists to avoid. Plan §3.
- `kafka.py` + `docker-compose.yml` — two topics keyed for what each must
  guarantee: `quotes.QQQ` (6 partitions, per-contract ordering, parallel
  consumption) and `chain.QQQ` (1 partition, **total ordering** of whole-chain
  snapshots, because a trading decision needs a coherent instant rather than
  interleaved per-contract updates). Topic auto-creation is disabled so
  partition counts stay an explicit design decision. Plan §11.
- `sessions.py`, `ids.py`, `models.py` — session calendar, run identifiers,
  shared row schemas.

**Phase 1 — `src/olv/feed/` and `src/olv/state/archive.py`**

- `client.py` — the `QuoteFeed` protocol is the vendor boundary; everything
  downstream is vendor-independent, so a broker drops in as one class. Greeks
  and IV are **excluded from the interface on purpose**: taking them from a
  vendor imports that vendor's clock, rate and dividend conventions, and the
  clock alone moves `sigma*sqrt(tau)` by 2.31x at 0DTE. Plan §3, §9.
- `universe.py` — the watch set is rebuilt **every snapshot**, not once per
  session, because spot moves and a band pinned at the open drifts off centre
  by the afternoon, quietly dropping the strikes that matter.
- `gate.py` — quote hygiene. Rejections are **counted and published, never
  silently dropped**: at 0DTE the wings go untradeable for stretches of the
  session, so how often the strategy simply could not have traded is a
  first-class result. Plan §9.
- `producer.py` — publishes each validated snapshot to both topics.
- `synthetic.py` — synthetic feed standing in for the vendor. Every row is
  stamped `feed_source`, so fabricated data is self-identifying in storage
  forever and cannot be mistaken for an observed session.
- `state/archive.py` — Kafka to Parquet. Offsets are committed **only after** a
  batch is durably written, never automatically. Explicitly at-least-once: a
  duplicate row is recoverable by deduplicating on `(symbol, quote_ts)`,
  whereas a lost row is gone and the session cannot be re-observed. Plan §11,
  §12.

## Not yet built

- `src/olv/strategy/`, `src/olv/broker/`, `src/olv/reporting/` are empty
  `__init__.py` files — no strategies, no fill model, no reports.
- **No vendor feed client.** Deliberately last: everything downstream of
  `olv.feed.client.QuoteFeed` is vendor-independent, and until a paper account
  exists the synthetic feed exercises the whole path — including the crossed
  markets, absent bids and stale prints a real feed produces only occasionally
  and never on demand.
- **No real recorded session yet.** All data so far is synthetic.
- The archiver writes to a local `Path`, **not S3**. The
  `s3://<bucket>/live/chains/QQQ/date=<YYYY-MM-DD>/` prefix in plan §12 does
  not exist yet.
- The archiver has **no CLI entrypoint** (`python -m` target). It is currently
  driven only from tests; `record.py` and `kafka_admin.py` are the only
  runnable modules.
- No DynamoDB, no transactional offset-with-state, no broker credentials.
- The backtest-lab dependency **is** now in `pyproject.toml`, pinned to
  `v0.2.0` — a tag that does not exist until PR #2 merges. Nothing in `src/`
  imports the pricer yet; only the shared clock is in use.

## Immediate next action

**Merge `npComplete21/options-backtest-lab#2`, then tag `v0.2.0` from its
`main`.** That tag is what `pyproject.toml` here already pins, so until it
exists `make install` cannot resolve the dependency from a clean checkout.
(Everything is verified against a local install of the same commit; only the
pin target is outstanding.)

After the tag, Phase 2 proper starts: back out implied vol from the recorded
mid using `obl.pricing.black_scholes` through `obl.timebase`, then calibrate
the intraday vol curve that `VolWeightedClock` is waiting for.

### What was done to get here (2026-09-09)

Both blockers previously listed here are closed in code:

1. **backtest-lab's work is off the branch and installable.** Phases 2a/2b/3 —
   instruments, ingestion, selectors, the strategy DSL — plus the clock, are in
   PR #2. That repo also turned out not to be importable *at all*: `timebase.py`
   was a top-level module under `src/` and never reached the wheel, the other
   packages were flattened to top-level names while the code imported `src.X`,
   and neither registry's YAML shipped. All three were invisible from inside a
   checkout because pytest puts the repo root on `sys.path`. It is now packaged
   as `obl`, verified by installing into a clean venv and importing with no
   source on the path.
2. **The tau clock is one module.** It was forked: both repos had written one,
   each calling itself the extraction point for the shared package. They were
   complementary, not rival — backtest-lab's was date-resolution and explicitly
   refused 0DTE; this repo's counted real session seconds and was the
   implementation that refusal was waiting for. Merged into `obl.timebase`:
   instants throughout, sessions injected so it stays pure-stdlib, backtest-lab's
   `id`/registry kept for `run_id` reproducibility. This repo's
   `src/olv/common/clock.py` is **deleted**; `tests/test_shared_clock.py` is the
   conformance test binding the shared clock to our real exchange calendar.

## Open decisions / deliberately deferred

From plan §14, plus what surfaced during Phases 0–1:

- **Kafka — RESOLVED 2026-09-08.** It stays because operating it is a learning
  goal, explicitly *not* because ~400k rows/day needs it. Recorded so nobody
  later "optimises" it away as dead weight.
- **Snapshot cadence** — 1s for strikes with an open position, 5s otherwise.
  Finer costs nothing in storage and cannot be recovered retroactively; not yet
  revisited.
- **Entry time** — fixed 09:45, or swept across the session? Plausibly the
  highest-sensitivity parameter in a 0DTE strategy; the recorder makes sweeping
  it free after the fact, so the decision is deferred to Phase 6.
- **Paid data** — reopens immediately if the broker paper tier turns out to
  serve indicative or delayed quotes (it then cannot measure spreads), and
  separately for *historical* 0DTE chains that would let backtest-lab replay
  years instead of starting the clock today.
- **Broker choice** — unresolved. Alpaca paper / Tradier sandbox to be
  evaluated on real OPRA bid/ask with sizes, snapshot cadence, rate limits,
  multi-leg 0DTE order support, and a documented paper fill engine.
- **Intraday vol-weighted clock** — deferred to Phase 2 on purpose; needs
  measured data.

## Key design decisions worth remembering

- **This repo is the primary data source, not a validator of a backtest.**
  backtest-lab prices synthetic chains from `^VXN` (30-day) with term shape
  anchored no shorter than `^VIX9D`; reaching tau ~ 0 from there is invention,
  not interpolation. So backtest-lab **cannot backtest 0DTE**, and every
  unrecorded session is permanently absent from the dataset. This is the single
  strongest argument for building the recorder first. Plan §0.
- **The tau clock is the largest modelling lever in the repo.** Calendar vs
  trading-hours differs 5.35x in tau and **2.31x in `sigma*sqrt(tau)`**, which
  moves which strikes get sold by better than a factor of two. Plan §3.
- **The scoring metric is `L/w` against the `L_max` ceiling, not cumulative
  P&L.** A losing day cannot average more than ~4.4x a winning one at
  `p = 0.85`. `p` and `L/w` are estimable in 40–60 sessions, which makes this a
  kill criterion resolvable in a quarter rather than a decade. Plan §1.
- **Transaction costs, not the vol premium, probably decide the outcome.** On a
  10-delta QQQ 0DTE option quoted $0.05/$0.09, the spread is plausibly larger
  than the entire volatility premium. Reporting must decompose P&L into
  `vol premium - spread - commission - slippage` so that conclusion is visible
  rather than inferred. Plan §10.
- **Natenberg's rules are encoded as engine constraints, not documentation** —
  e.g. any action increasing short quantity in an open position is rejected at
  config load. Plan §2.
- **The tournament includes a long ATM straddle as the null hypothesis**, since
  Natenberg argues the *other* side of a 0DTE short strangle is the
  positive-expectancy one. If it beats the short-premium book, that is the most
  valuable finding available here. Plan §2, §5.
- **Strike selection happens at entry only**, and management triggers are never
  per-leg delta — as tau approaches 0 delta becomes a step function, so a
  "10-delta strike" at 15:30 is nearly at the money. Plan §3.
- **Defined-risk and undefined-risk max losses are never rendered in the same
  column without a marker.** The defined-risk book's max loss is a fact; the
  naked book's is a hope. Plan §6.
- **The tau clock is shared code, not this repo's.** It lives in `obl.timebase`,
  pinned by tag. Adding a local clock or computing a year fraction inline
  recreates the fork closed on 2026-09-09. Bumping the pin can move every
  delta-selected strike, so it is a modelling change, not a dependency bump.
  Plan §3, and `CLAUDE.md` Conventions.
- **An expiry in the past returns tau `0.0` rather than raising** — a deliberate
  loosening when the clocks merged, since backtest-lab needs zero for contracts
  rolled off a chain. The guard moved rather than vanished: an expired contract
  reaching a live decision is a *selection* bug, and `clock_ratio` still refuses
  a degenerate denominator.
- **The shared clock takes sessions injected and stays pure-stdlib**, so
  depending on it pulls no `pandas_market_calendars`, numpy or polars into a
  streaming process. `olv.common.sessions.SessionCalendar` is our side of that
  seam.

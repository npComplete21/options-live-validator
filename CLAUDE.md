# options-live-validator

## Purpose
Streaming, paper-trading project. Take a strategy that already looked good
in `options-backtest-lab`, run it against live/near-live market data in
**paper trading mode only**, and track how it actually performs before any
consideration of real capital.

This repo may eventually place PAPER orders via a broker's paper endpoint.
It must never place real orders unless explicitly and separately authorized
— treat any code path touching a "live"/production API key as something to
flag loudly, not assume.

## Reference material
- `../options-research/notes/` — which strategies/tickers earned the right
  to be tested live, and why.
- `../options-backtest-lab/` — the backtested parameter set being validated
  here should match what's being paper-traded; note the source run_id.

## Architecture
1. **Market data feed** — broker/data API (e.g. Alpaca paper, Tradier
   sandbox) providing live or delayed quotes for the underlying + options
   chain.
2. **Kafka** — a producer ingests the feed into a topic
   (`quotes.<ticker>`); a consumer runs the strategy + adjustment logic and
   emits paper orders/fills as events.
3. **State store** — DynamoDB (or RDS) holding open positions, adjustment
   history, running P&L. Must be resumable after a restart without losing
   position state.
4. **Reporting** — periodic snapshot of paper P&L per strategy, comparable
   across strategies being tested concurrently.

## Tech stack
- Python 3.11
- Kafka: self-hosted on a small EC2 instance to start (or Amazon MSK
  Serverless once the setup is proven)
- DynamoDB for position/state persistence
- S3 for event/log archival
- Broker: paper trading endpoint only (confirm which broker before wiring
  credentials)

## Conventions
- All broker/API credentials via environment variables or AWS Secrets
  Manager — never hardcoded, never committed.
- Every strategy run is tagged with the backtest `run_id` it corresponds to.
- Paper vs. live mode must be an explicit, impossible-to-miss config flag,
  defaulting to paper. As implemented in `src/olv/common/mode.py`: an enum,
  never a bool; `resolve_endpoint()` is the only thing permitted to construct
  a broker base URL, so it cannot be set independently of the mode; and **no
  production hostname is committed to this repo** — the paper host is a
  literal, the live host must come from the environment and has no default.
  A test greps `src/` to keep that true, so never add a live URL literal.
- **The tau clock is not in this repo.** It lives in `obl.timebase`, shared
  with `options-backtest-lab` and pinned by tag in `pyproject.toml`; plan §3
  makes an identical clock in both repos a hard requirement. Do not add a local
  clock, and do not compute a year fraction inline — both recreate the fork
  that was closed on 2026-09-09. `olv.common.sessions.SessionCalendar` is our
  implementation of the clock's `SessionSource` protocol; the clock is
  pure-stdlib and takes sessions injected, so keep it that way.
  Bumping the pin can move every delta-selected strike: treat it as a modelling
  change and re-run `tests/test_shared_clock.py`.
- Never take greeks or implied vol from a market-data vendor. They embed the
  vendor's clock, rate and dividend conventions; the clock alone moves
  `sigma*sqrt(tau)` by 2.31x at 0DTE. Compute them from the recorded mid with
  backtest-lab's pricer.

## Out of scope for this repo
- Historical backtesting / synthetic option pricing → that's
  `options-backtest-lab`.
- Strategy/ETF research → that's `options-research`.

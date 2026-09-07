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
  defaulting to paper.

## Out of scope for this repo
- Historical backtesting / synthetic option pricing → that's
  `options-backtest-lab`.
- Strategy/ETF research → that's `options-research`.

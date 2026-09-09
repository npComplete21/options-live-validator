# options-live-validator

Streaming 0DTE chain recorder and paper-trading strategy tournament for QQQ.

Part of a three-repo program — see [`CLAUDE.md`](CLAUDE.md) for the boundaries
between this, `options-backtest-lab` and `options-research`, and
[`docs/IMPLEMENTATION_PLAN.md`](docs/IMPLEMENTATION_PLAN.md) for the design.

## What this is for

Five option strategies run concurrently against the same live QQQ 0DTE feed
every session, sized to equal risk, and compared. The target is 12–15% a year
on a $250k book over 252 sessions.

Two things are worth knowing before reading further:

- **Paper only.** This repo has no path to production. The live broker
  hostname is not committed to it, and a test asserts that stays true.
- **The recorder matters more than the trader.** `options-backtest-lab` prices
  synthetic chains from a 30-day vol index and cannot reach 0DTE, so the
  recorded chain here is the primary dataset for this program rather than a
  by-product. See section 0 of the plan.

## Quick start

```bash
make install
make kafka-up      # single-broker KRaft via docker compose
make topics        # auto-creation is disabled; topology is explicit
make test          # unit tests, no broker required
make test-all      # adds integration tests against the running broker
```

## Layout

```
src/olv/
  common/    config, execution-mode guard, tau clock, sessions, ids, Kafka topology
  feed/      vendor client, watch set, quote hygiene gate, producer
  strategy/  consumers, strategy definitions, residuals
  broker/    paper order submission, conservative fill model
  state/     DynamoDB state, S3/Parquet archive
  reporting/ tournament scorecard, cost decomposition, surface report
```

## Status

Phase 0 complete: execution-mode guard, τ clock, run identifiers, Kafka
topology and provisioning, local broker. Phase 1 (the recorder) is next; see
section 15 of the plan for the build order and gates.

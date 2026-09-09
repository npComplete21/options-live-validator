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
  common/    config, execution-mode guard, sessions, ids, Kafka topology
  feed/      vendor client, watch set, quote hygiene gate, producer
  strategy/  consumers, strategy definitions, residuals
  broker/    paper order submission, conservative fill model
  state/     DynamoDB state, S3/Parquet archive
  reporting/ tournament scorecard, cost decomposition, surface report
```

## Status

Current stage, what's built and why, and the immediate next action live in
[`docs/PROJECT_STATUS.md`](docs/PROJECT_STATUS.md) — a living snapshot,
rewritten rather than appended to, so there is one place to look and nothing
to cross-check.

In short: Phases 0 and 1 are merged, and the recorder runs end to end today
against a synthetic feed.

```bash
make kafka-up && make topics
.venv/bin/python -m olv.record --snapshots 8 --interval 0 --defect-rate 0.08
```

The vendor client is the one remaining piece of Phase 1, and it is deliberately
the last: everything downstream of `olv.feed.client.QuoteFeed` is
vendor-independent, so a broker drops in as a single class once a paper account
exists. Until then the synthetic feed exercises the whole path — including the
crossed markets, absent bids and stale prints a real feed produces only
occasionally and never on demand. Every row is stamped `feed_source`, so
synthetic data is self-identifying in storage forever.

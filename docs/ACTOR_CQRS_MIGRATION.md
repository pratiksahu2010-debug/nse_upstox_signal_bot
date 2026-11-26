# Actor-Based CQRS + Event Sourcing Migration

## Target

Adopt a modular-monolith architecture combining bounded virtual actors with a PostgreSQL event store. This is an additive foundation; it does not change live signal decisions.

## Design

- A virtual actor is keyed by symbol. Its mailbox serializes events for that symbol.
- A bounded shared worker pool processes different symbols concurrently. Do not create 150 OS threads.
- PostgreSQL `domain_events` is the durable append-only event ledger. Event IDs and (aggregate, sequence) are unique.
- PostgreSQL `actor_snapshots` stores monotonic recovery checkpoints.
- Read projections remain separate from the event ledger; CQRS queries must never become strategy inputs.
- Actor mailboxes are ephemeral. Persist an event before enqueueing it; after restart restore a snapshot and replay later events.
- Event replay is side-effect free. Replay must never send Telegram messages or place broker orders.
- The existing signals table and its unique constraints remain authoritative for current alert deduplication during migration.

## Rollout phases

1. **Foundation (this change):** event envelope, append/replay/snapshot persistence, bounded per-symbol actor runtime and isolated tests. No live engine wiring.
2. **Shadow integration:** feed the same completed 5-minute candle events to actors while the current scanner remains authoritative. Compare decisions; do not publish shadow signals.
3. **Projection migration:** build current-state and analytics read projections from events, with reconciliation against existing `signals` and `normal_setup_state` records.
4. **Controlled cutover:** only after parity, restart/replay, idempotency, backpressure and failure-injection tests pass; keep a rollback switch.
5. **Retirement:** remove old state persistence only after production parity is demonstrated.

## Non-negotiable invariants

- Completed 5-minute candles only; no partial candle or look-ahead.
- Existing indicators, score thresholds, ADX floor, entry/exit and risk logic remain unchanged.
- Signal-only operation; `orders_enabled=false`.
- Per-symbol ordering, durable event IDs, monotonic aggregate sequence and snapshots.
- PostgreSQL remains required in production. Never fall back to volatile memory when configured production persistence is unavailable.
- No automatic merge or Render deployment as part of this migration.

# MT5 Agent Control Plane Contract V1

This directory defines the TradeMove control-plane side of the V1 wire contract. A copy is kept in
both repositories:

- `trademove/contracts/mt5-agent-v1/`
- `tradejournal-mt5-cloud-worker/contracts/mt5-agent-v1/`

There is no shared package linking them (separate repos, separate languages: Deno/TS Edge
Function vs. Python Windows Agent). `SHA256SUMS` pins the two wire files;
`scripts/verify-mt5-contract-sync.mjs` also compares README and manifest with the worker copy.

## Compatibility status

The local snapshots are synchronized as of 2026-10-12, including the additive
`heartbeat_failed` error for a transient database failure. Run both contract suites and the
read-only peer check before either repository is released. Identical files and mocked tests
do not prove Windows/MT5 end-to-end compatibility or authorize a VPS rollout.

## Source of truth

`schema.json` is derived directly from the real implementation:

- `trademove/supabase/functions/trading-agent/index.ts` (routes, request/response shapes)
- `trademove/supabase/migrations/20260715135432_mt5_agent_control_plane.sql` (job_type, history_mode, status enums)
- `trademove/supabase/migrations/20260729172632_mt5_job_progress_events.sql` (lease-bound progress events)
- `trademove/supabase/migrations/20260814074954_mt5_event_driven_control_plane.sql` (private Realtime wake-up)

If you change the Edge Function's request/response shape, update `schema.json` and
`fixtures.json` here FIRST, copy both files into the other repository, then update
each repository's contract test and both checksum manifests. Preserve byte identity for all
four contract files; never rebuild checksums automatically during verification.

Before either repository is released, compare the complete directory and run both test suites:

```sh
node scripts/verify-mt5-contract-sync.mjs --peer-dir ../tradejournal-mt5-cloud-worker/contracts/mt5-agent-v1
npm test -- supabase/functions/trading-agent/contract.test.ts
cd ../tradejournal-mt5-cloud-worker
python3 scripts/verify_contract_sync.py --peer-dir ../trademove/contracts/mt5-agent-v1
python3 -m pytest tests/windows/test_contract.py tests/test_contract_snapshot.py
```

A non-empty diff blocks a coordinated control-plane/Agent rollout unless the difference is an
explicitly reviewed, backwards-compatible documentation-only change.

## Contract tests

- `trademove`: `supabase/functions/trading-agent/contract.test.ts` (Vitest, validates `fixtures.json` against `schema.json` with ajv, and asserts the actual `index.ts` route handler produces schema-valid responses against fixture requests via dependency-injected fakes).
- `tradejournal-mt5-cloud-worker`: `tests/windows/test_contract.py` (pytest, validates `fixtures.json` against `schema.json` with `jsonschema`, and asserts `AgentApiClient`'s request bodies validate against the request schemas).

## `payload` shape by job_type

`schema.json` validates the secret-bearing `provision` envelope strictly. The other job payloads
remain opaque objects because the durable database queue may add non-secret command metadata.

- `provision`: `{ credential_envelope: {alg, iv, ciphertext}, expected_login: string, expected_server: string, broker_label: string | null }`.
  It also includes a fresh `bridge_token` for event ingestion. `credential_envelope` decrypts
  (via `MT5_PROVISIONING_ENCRYPTION_KEY`, shared out-of-band with
  the Agent) to `{ investor_password: string }`. `expected_login`/`expected_server` travel
  unencrypted -- they are not secrets, already plaintext on `trading_connections`, and the Agent
  needs them before it can verify which account it just authenticated into. `broker_label=null`
  asks the Windows Agent for fail-closed, suggestion-only identity resolution; the result becomes
  public only after a successful investor login and an atomic provenance check.
- `historical_sync` / `deprovision`: `payload` is not required. The Agent reuses whatever
  `mt5_login`/`mt5_server`/`mt5_investor_password` it already persisted to DPAPI during the
  connection's original `provision` job.

## Progress

`POST jobs/{job_id}/progress` accepts only an allowlisted phase, phase status, and optional
machine-readable detail code. It never accepts arbitrary metadata or free text. The database
checks the active agent lease atomically and makes duplicate delivery idempotent.

Deploy the additive database/Edge contract before deploying an Agent that emits progress.
Older Agents remain compatible because the existing claim, heartbeat, and transition routes do
not change.

## Historical trade batches

`POST jobs/{job_id}/history` accepts 1–50 normalized MT5 `trade_opened`/`trade_closed`
events. The endpoint requires the Agent's `jobs:update` scope and the database revalidates the
active `historical_sync` lease before atomically ingesting the batch. Event IDs and imported
trade IDs are deterministic, so a retry is idempotent. Historical openings populate the review
inbox without generating one notification per old trade.

## Fast history file import

For a complete initial history, the Agent creates one deterministic `history.json.gz` document,
prepares a lease-bound upload, sends it directly to the private `mt5-history-imports` bucket, and
commits it once. The server derives the object key as
`{user_id}/{connection_id}/{job_id}/history.json.gz`; the Agent cannot select another user's path.
The commit verifies compressed hash/size, decompressed size, document identity and every event,
then imports the complete array in one database transaction. A successful import is idempotently
recorded and the transient Storage object is deleted.

## Private Realtime wake-up

`POST session` exchanges the opaque DPAPI Agent token for a one-hour JWT and the two authorized
private topics. Broadcast is not a queue and never carries credentials or the job payload: after
startup, reconnect, or `command_available`, the Agent drains `claim` until HTTP 204. Supported
durable job types are `provision`, `historical_sync`, and `deprovision`; `live_sync` is historical
audit data only and is never claimable.

## Versioning

`api_version` is currently the literal string `"1"`. A breaking change to any
request/response shape must introduce `"2"` and a new `$defs` set here rather than
mutating the V1 definitions in place, since a Windows Agent and the control plane
can be deployed independently and must both keep working against whichever
`api_version` they each currently speak.

## Ingestion backpressure is separate from job leases

`trading-mt5-events` is a separate endpoint, not a job transition in this schema. HTTP 429
there means the new event/heartbeat was not saved; honor its bounded `Retry-After` and keep
the original event ID and payload until an explicit JSON `status: ok` or `status: duplicate`
acknowledgement. A deferred heartbeat is a local scheduling decision, not server liveness.

Do not turn `heartbeat_failed` on the control-plane job route into `lease_lost`, and do not
apply ingestion backpressure to claim/progress/lease renewal. The new rate policy stays OFF
until the coordinated release and authenticated/demo E2E gates have passed.

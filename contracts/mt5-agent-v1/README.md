# MT5 Agent Control Plane Contract V1

This directory is duplicated byte-for-byte in both repositories:

- `tradejournal-drp/contracts/mt5-agent-v1/`
- `tradejournal-mt5-cloud-worker/contracts/mt5-agent-v1/`

There is no shared package linking them (separate repos, separate languages: Deno/TS
edge function vs. Python Windows Agent), so this is a **manually synced** contract.

## Source of truth

`schema.json` is derived directly from the real implementation:

- `tradejournal-drp/supabase/functions/trading-agent/index.ts` (routes, request/response shapes)
- `tradejournal-drp/supabase/migrations/20260715135432_mt5_agent_control_plane.sql` (job_type, history_mode, status enums)

If you change the Edge Function's request/response shape, update `schema.json` and
`fixtures.json` here FIRST, copy both files into the other repository, then update
each repository's contract test.

## Contract tests

- `tradejournal-drp`: `supabase/functions/trading-agent/contract.test.ts` (Vitest, validates `fixtures.json` against `schema.json` with ajv, and asserts the actual `index.ts` route handler produces schema-valid responses against fixture requests via dependency-injected fakes).
- `tradejournal-mt5-cloud-worker`: `tests/windows/test_contract.py` (pytest, validates `fixtures.json` against `schema.json` with `jsonschema`, and asserts `AgentApiClient`'s request bodies validate against the request schemas).

## `payload` shape by job_type

`schema.json` deliberately leaves `payload` as a generic `object` (it is intentionally opaque to
the control plane -- see `mt5_provisioning_jobs.payload jsonb`). By convention, the actual
producer/consumer (`request-mt5-connection/index.ts` and `windows_agent/real_handlers.py`) agree
on this shape:

- `provision`: `{ credential_envelope: {alg, iv, ciphertext}, expected_login: string, expected_server: string }`.
  `credential_envelope` decrypts (via `MT5_PROVISIONING_ENCRYPTION_KEY`, shared out-of-band with
  the Agent) to `{ investor_password: string }`. `expected_login`/`expected_server` travel
  unencrypted -- they are not secrets, already plaintext on `trading_connections`, and the Agent
  needs them before it can verify which account it just authenticated into.
- `historical_sync` / `deprovision`: `payload` is not required. The Agent reuses whatever
  `mt5_login`/`mt5_server`/`mt5_investor_password` it already persisted to DPAPI during the
  connection's original `provision` job.

## Versioning

`api_version` is currently the literal string `"1"`. A breaking change to any
request/response shape must introduce `"2"` and a new `$defs` set here rather than
mutating the V1 definitions in place, since a Windows Agent and the control plane
can be deployed independently and must both keep working against whichever
`api_version` they each currently speak.

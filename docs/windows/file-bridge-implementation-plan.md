# Spec: Windows-native MT5 file bridge

## Objective

Make the Windows-native, read-only MQL5 file bridge the default MT5 ingestion path. An
isolated terminal runs `TradeJournalBridge.ex5`, writes atomic JSON snapshots under its
own `MQL5/Files/TradeJournal` directory, and the Windows agent imports those snapshots
without Python's `MetaTrader5` package, an HTTP bridge, trading operations, or user GUI
steps. `PythonDirectMt5Adapter` stays in the repository as an explicitly disabled future
fallback, not the default runtime path.

## Constraints

- No credentials, production calls, Docker/Wine, legacy builds, network listeners, commits,
  pushes, deployment, firewall/RDP/SSH/Windows changes, or trading API calls.
- The EA is read-only and receives no passwords or tokens through its file payloads.
- Provisioning receives the investor password only through the existing encrypted envelope,
  saves it in DPAPI, writes a protected temporary startup config, and removes that config
  immediately after terminal bootstrap.
- Changes remain small and independently testable. New source files stay below 500 lines.

## Existing components reused

- `TradeJournalBridge.mq5` for atomic writes, snapshots and transaction events.
- `bridge/files/file_bridge.py` for file parsing, freshness, cursor and deal-dedup semantics;
  it remains the legacy HTTP consumer and is not started by the Windows agent.
- `NativeMt5Runtime`, `InstanceProvisioner`, DPAPI secret store, `HistorySync`, `LiveSync`,
  `PersistentDedup` and the real daemon handlers.

## Architecture decision

`Mql5FileMt5Adapter` is the default adapter for real handlers. It reads only the local files
from one isolated portable terminal. It validates a versioned envelope, checks heartbeat
freshness and account/server identity, persists a bounded event checkpoint, and ignores
partial or corrupt JSON until the next poll. The adapter exposes the existing sync interface;
therefore `HistorySync` and `LiveSync` do not need a protocol rewrite.

## Tasks and acceptance checks

- [ ] Version the EA snapshot envelopes and add atomic `deals`, `candles/*`, `events/*` output.
  - Verify: static guard rejects every prohibited trading symbol and EA compilation succeeds.
- [ ] Add the local file adapter and keep its state bounded and recoverable.
  - Verify: unit tests cover schema, stale/corrupt files, identity mismatch, dedup and resume.
- [ ] Make real provision/history/deprovision handlers choose native file mode by default.
  - Verify: mock control plane → daemon → file producer → sync → deprovision E2E passes.
- [ ] Prepare a non-interactive Windows template and secure customer-flow script.
  - Verify: no password can appear in process arguments or report output.
- [ ] Compile on the VPS and execute a no-login terminal/EA heartbeat test.
  - Verify: reports record the command outcome, EX5 hash, generated file validation and cleanup.

## Commands

```powershell
python -m pytest tests/test_mql5_ea_no_trading.py tests/windows/test_native_mt5_runtime.py tests/windows/test_mql5_file_adapter.py
python -m pytest tests/windows/test_file_bridge_daemon_e2e.py
```

The final VPS commands are written into the handoff only after the local mock tests pass. They
must never include account credentials or make a login attempt during the no-login verification.

## Success criteria

1. The compiled EA has no static trading API violations.
2. A generic terminal can create a valid, fresh, read-only heartbeat without an account login.
3. The real handlers use no Python `MetaTrader5` import in their default path.
4. A mocked daemon flow reaches `connected`, imports history/live events idempotently, then
   deprovisions only its own process and state.
5. The requested Windows report and handoff contain sanitized evidence and next steps.

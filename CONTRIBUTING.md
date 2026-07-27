# Contributing

## Safety first

- Never commit MT5 binaries, credentials, endpoint captures, ETL/EVTX files or
  private runtime directories.
- Do not enable actual launch, firewall, WFP, bootstrap or registry promotion
  as part of an unrelated change.
- Keep AI output suggestion-only; MT5 evidence is required for verification.
- Preserve read-only behavior and absence of trading primitives.

## Change boundaries

- Production code must not import from `lab/`.
- New Windows runtime behavior needs Windows tests.
- Do not introduce a second MT5 runtime beside the Windows-native file path
  without an architecture decision and security review.
- Contract V1 changes require a new API version when breaking.

## Validation

Core:

```bash
python3 -m pytest tests --ignore=tests/windows -m "not research" -q
python3 scripts/verify_contract_sync.py
```

Lab:

```bash
python3 -m unittest discover -s lab/mt5_direct_endpoint/tests -q
python3 -m unittest discover -s lab/mt5_direct_endpoint/mql5/tests -q
python3 -m unittest discover -s lab/mt5_direct_endpoint/windows/tests -p 'test_*.py' -q
```

Windows Agent and .NET harnesses are validated by their dedicated Windows
workflows. Do not replace a platform failure with a broad skip or
`continue-on-error`.

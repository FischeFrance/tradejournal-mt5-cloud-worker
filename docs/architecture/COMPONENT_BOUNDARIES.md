# Component boundaries

## Production path

`windows_agent/` owns account provisioning, DPAPI integration, process
lifecycle and control-plane jobs. It may reuse pure domain logic from
`worker/`, but must not import from `lab/`.

`worker/` owns event detection, normalization, outbox and delivery. It must
not manage Windows processes or credentials.

`mt5/experts/` and `windows_agent/worker/mql5_file_adapter.py` form the
read-only file boundary with MT5. No trading primitive is allowed across this
boundary.

## Onboarding incubation

The following components are incubating under `lab/mt5_direct_endpoint`:

- endpoint registry;
- credential-free config dry-run;
- account onboarding state machine;
- broker identity resolver;
- MT5 Wizard Automation.

They are not production dependencies. Promotion into `windows_agent` requires:

1. a versioned input/output contract;
2. Windows CI;
3. a threat-model review;
4. no relaxation of `HARD_DISABLED`;
5. an explicit integration patch without compatibility copies.

Until then, production code must not import these modules.

## Verification lab

The permanent laboratory scope is:

- C0–C5 contracts and schemas;
- evidence assembler and verifier;
- JobHarness and C012 Coordinator;
- ETW/WFP planning and sanitization;
- reproducible synthetic fixtures.

Captured data, credentials, MT5 binaries and private artifacts never belong in
the repository or review package.

## Deliberately unsupported paths

Ubuntu/Docker/Wine provisioning, HTTP bridge runtimes, market-data research
containers and direct Python IPC are not part of this repository. Reintroducing
one of those paths requires a new architecture decision and security review.

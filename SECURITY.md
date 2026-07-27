# Security policy

Report suspected credential exposure, unauthorized trading capability,
process-escape, path traversal or evidence-integrity issues privately to the
repository owner. Do not include credentials, raw captures or customer data in
a public issue.

## Supported path

Security fixes target the single Windows-native file architecture documented
by ADR-001. Docker/Wine, bridge HTTP and direct Python MT5 IPC are unsupported
and intentionally absent.

## Sensitive material

Never attach:

- passwords, API keys or token values;
- account exports;
- raw ETL, EVTX, PCAP or WFP data;
- complete process command lines or environment dumps;
- MT5 terminal, MetaEditor, MetaTester or broker binaries.

Use sanitized reproductions and one-way digests. A digest is an integrity
commitment, not proof of trusted execution.

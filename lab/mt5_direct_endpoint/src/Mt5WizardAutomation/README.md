# MT5 Wizard Automation

Fail-closed orchestrator for the MT5 “Find your broker” UI.

## Authority model

Input is suggestion-only:

- `SearchText`: text typed into the broker search box;
- `SuggestedBrokerLabel`: non-authoritative AI hint;
- `ExpectedServerName`: exact server supplied by the caller.

The orchestrator accepts a selection only when exactly one MT5 result contains
`ExpectedServerName`, compared case-insensitively after trimming. Zero matches
fail as `ServerNotFound`; multiple matches fail as `AmbiguousBroker`. The
selected broker label is read back from MT5 and is never replaced by the AI
label.

The JSON shape is pinned by
`schemas/broker-wizard-request.schema.json`.

## Runtime gate

`Mt5WizardRuntimeGate.ActualUiAutomationEnabled` is `false`. The gate executes
before PID inspection or FlaUI attachment. Tests use `FakeDriver` only; they
do not start MT5, MetaEditor or MetaTester.

No production profile is bundled. A future profile must be acquired from a
controlled MT5 build, contain stable selectors, be reviewed independently and
be authorized separately before the gate can change.

## Tests

On Windows:

```powershell
dotnet build `
  lab/mt5_direct_endpoint/src/Mt5WizardAutomation/tests/Mt5WizardAutomation.Tests/Mt5WizardAutomation.Tests.csproj `
  --configuration Release --nologo

dotnet run `
  --project lab/mt5_direct_endpoint/src/Mt5WizardAutomation/tests/Mt5WizardAutomation.Tests/Mt5WizardAutomation.Tests.csproj `
  --configuration Release --no-build
```

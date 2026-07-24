[CmdletBinding()]
param(
    [switch]$RunWindowsProcessSmoke,
    [switch]$RunWindowsRuntimeSmoke
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$projectRoot = Split-Path -Parent $PSScriptRoot
$testsRoot = Join-Path $projectRoot 'tests'
$testDirectory = Join-Path $testsRoot 'JobHarness.SmokeTests'
$testProject = Join-Path $testDirectory 'JobHarness.SmokeTests.csproj'

dotnet build $testProject --configuration Release --nologo
if ($LASTEXITCODE -ne 0) {
    throw "dotnet build failed with exit code $LASTEXITCODE."
}

$previousLiveSmoke = [Environment]::GetEnvironmentVariable('JOBHARNESS_RUN_LIVE_SMOKE', 'Process')
Remove-Item Env:\JOBHARNESS_RUN_LIVE_SMOKE -ErrorAction SilentlyContinue
$previousRuntimeSmoke = [Environment]::GetEnvironmentVariable('JOBHARNESS_RUN_RUNTIME_SMOKE', 'Process')
Remove-Item Env:\JOBHARNESS_RUN_RUNTIME_SMOKE -ErrorAction SilentlyContinue

try {
    if ($RunWindowsProcessSmoke) {
        if ($env:OS -ne 'Windows_NT') {
            throw '-RunWindowsProcessSmoke is supported only on Windows.'
        }

        $env:JOBHARNESS_RUN_LIVE_SMOKE = '1'
    }

    if ($RunWindowsRuntimeSmoke) {
        if ($env:OS -ne 'Windows_NT') {
            throw '-RunWindowsRuntimeSmoke is supported only on Windows.'
        }

        $env:JOBHARNESS_RUN_RUNTIME_SMOKE = '1'
    }

    dotnet run --project $testProject --configuration Release --no-build
    if ($LASTEXITCODE -ne 0) {
        throw "dotnet run failed with exit code $LASTEXITCODE."
    }
}
finally {
    if ($null -eq $previousLiveSmoke) {
        Remove-Item Env:\JOBHARNESS_RUN_LIVE_SMOKE -ErrorAction SilentlyContinue
    }
    else {
        $env:JOBHARNESS_RUN_LIVE_SMOKE = $previousLiveSmoke
    }
    if ($null -eq $previousRuntimeSmoke) {
        Remove-Item Env:\JOBHARNESS_RUN_RUNTIME_SMOKE -ErrorAction SilentlyContinue
    }
    else {
        $env:JOBHARNESS_RUN_RUNTIME_SMOKE = $previousRuntimeSmoke
    }
}

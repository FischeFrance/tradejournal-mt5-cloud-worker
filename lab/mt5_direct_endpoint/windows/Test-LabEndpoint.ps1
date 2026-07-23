#requires -Version 5.1

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [string]$Endpoint,

    [int[]]$AllowedPort = @(),

    [string[]]$ResolvedAddress = @(),

    [switch]$AsJson
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

Import-Module (Join-Path $PSScriptRoot 'MT5DirectEndpoint.Lab.psm1') -Force

# Questa validazione e' intenzionalmente offline: non risolve DNS e non apre socket.
$result = Test-LabEndpoint `
    -Endpoint $Endpoint `
    -AllowedPort $AllowedPort `
    -ResolvedAddress $ResolvedAddress

if ($AsJson) {
    $result | ConvertTo-Json -Depth 8
}
else {
    $result
}


param(
    [Parameter(Mandatory = $true)]
    [string]$RequestPath,

    [Parameter(Mandatory = $true)]
    [string]$ResultPath
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

Add-Type @'
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Text;

public static class TradeJournalMt5AuthenticationDialog
{
    public delegate bool EnumWindowProc(IntPtr handle, IntPtr parameter);

    [DllImport("user32.dll")]
    private static extern bool EnumWindows(EnumWindowProc callback, IntPtr parameter);

    [DllImport("user32.dll")]
    private static extern bool EnumChildWindows(
        IntPtr parent,
        EnumWindowProc callback,
        IntPtr parameter);

    [DllImport("user32.dll")]
    private static extern uint GetWindowThreadProcessId(
        IntPtr handle,
        out uint processId);

    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    private static extern int GetWindowText(
        IntPtr handle,
        StringBuilder value,
        int maximumLength);

    private static string WindowText(IntPtr handle)
    {
        var value = new StringBuilder(1024);
        GetWindowText(handle, value, value.Capacity);
        return value.ToString();
    }

    public static bool HasAuthenticationFailure(uint expectedProcessId)
    {
        var detected = false;
        EnumWindows(delegate(IntPtr handle, IntPtr ignored)
        {
            uint processId;
            GetWindowThreadProcessId(handle, out processId);
            if (processId != expectedProcessId)
                return true;

            var values = new List<string>();
            values.Add(WindowText(handle));
            EnumChildWindows(handle, delegate(IntPtr child, IntPtr childIgnored)
            {
                values.Add(WindowText(child));
                return true;
            }, IntPtr.Zero);
            var text = string.Join("\n", values);
            var failures = new[] {
                "invalid account",
                "authorization failed",
                "incorrect password",
                "account disabled"
            };
            foreach (var failure in failures)
            {
                if (text.IndexOf(failure, StringComparison.OrdinalIgnoreCase) >= 0)
                {
                    detected = true;
                    return false;
                }
            }
            return true;
        }, IntPtr.Zero);
        return detected;
    }
}
'@

function Write-AuthenticationResult {
    param([hashtable]$Value)

    $parent = Split-Path -Parent $ResultPath
    if (-not $parent -or -not (Test-Path -LiteralPath $parent -PathType Container)) {
        throw 'authentication_result_parent_missing'
    }
    $temporary = "$ResultPath.tmp"
    try {
        $Value | ConvertTo-Json -Compress |
            Set-Content -LiteralPath $temporary -Encoding UTF8
        Move-Item -LiteralPath $temporary -Destination $ResultPath -Force
    }
    finally {
        Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue
    }
}

try {
    $requestItem = Get-Item -LiteralPath $RequestPath -Force
    if (
        $requestItem.PSIsContainer -or
        ($requestItem.Attributes -band [IO.FileAttributes]::ReparsePoint)
    ) {
        throw 'authentication_request_invalid'
    }
    $request = Get-Content -LiteralPath $RequestPath -Raw -Encoding UTF8 |
        ConvertFrom-Json
    if (
        $request.schema_version -ne 1 -or
        [int64]$request.process_id -le 0 -or
        [int64]$request.creation_time_unix_ms -le 0 -or
        [string]::IsNullOrWhiteSpace([string]$request.expected_executable) -or
        [int]$request.timeout_seconds -lt 1 -or
        [int]$request.timeout_seconds -gt 300
    ) {
        throw 'authentication_request_invalid'
    }

    $process = Get-Process -Id ([int]$request.process_id) -ErrorAction Stop
    $observedCreationTime = [DateTimeOffset]::new(
        $process.StartTime.ToUniversalTime()
    ).ToUnixTimeMilliseconds()
    if (
        -not [string]::Equals(
            $process.Path,
            [string]$request.expected_executable,
            [StringComparison]::OrdinalIgnoreCase
        ) -or
        $observedCreationTime -ne [int64]$request.creation_time_unix_ms
    ) {
        throw 'authentication_process_identity_mismatch'
    }

    $deadline = (Get-Date).AddSeconds([int]$request.timeout_seconds)
    do {
        if (
            [TradeJournalMt5AuthenticationDialog]::HasAuthenticationFailure(
                [uint32]$request.process_id
            )
        ) {
            Write-AuthenticationResult @{
                schema_version = 1
                success = $true
                detected = $true
                error_code = 'authorization_failed'
                process_id = [int]$request.process_id
                creation_time_unix_ms = [int64]$request.creation_time_unix_ms
            }
            exit 0
        }
        Start-Sleep -Milliseconds 150
    } while ((Get-Date) -lt $deadline)

    Write-AuthenticationResult @{
        schema_version = 1
        success = $true
        detected = $false
        error_code = $null
        process_id = [int]$request.process_id
        creation_time_unix_ms = [int64]$request.creation_time_unix_ms
    }
    exit 0
}
catch {
    try {
        Write-AuthenticationResult @{
            schema_version = 1
            success = $false
            detected = $false
            error_code = 'authentication_monitor_failed'
            process_id = 0
            creation_time_unix_ms = 0
        }
    }
    catch {
        # A missing result is treated as unavailable by the service-side fallback.
    }
    exit 1
}

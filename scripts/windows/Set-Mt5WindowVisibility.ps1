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

public static class TradeJournalMt5WindowVisibility
{
    public delegate bool EnumWindowProc(IntPtr handle, IntPtr parameter);

    [DllImport("user32.dll")]
    private static extern bool EnumWindows(EnumWindowProc callback, IntPtr parameter);

    [DllImport("user32.dll")]
    private static extern uint GetWindowThreadProcessId(
        IntPtr handle,
        out uint processId);

    [DllImport("user32.dll")]
    private static extern bool IsWindowVisible(IntPtr handle);

    [DllImport("user32.dll")]
    private static extern bool ShowWindowAsync(IntPtr handle, int command);

    public static IntPtr[] WindowsForProcess(uint expectedProcessId)
    {
        var handles = new List<IntPtr>();
        EnumWindows(delegate(IntPtr handle, IntPtr ignored)
        {
            uint processId;
            GetWindowThreadProcessId(handle, out processId);
            if (processId == expectedProcessId)
                handles.Add(handle);
            return true;
        }, IntPtr.Zero);
        return handles.ToArray();
    }

    public static int VisibleCount(IntPtr[] handles)
    {
        var count = 0;
        foreach (var handle in handles)
            if (IsWindowVisible(handle))
                count++;
        return count;
    }

    public static void SetVisible(IntPtr[] handles, bool visible)
    {
        const int SW_HIDE = 0;
        const int SW_SHOWNA = 8;
        var command = visible ? SW_SHOWNA : SW_HIDE;
        foreach (var handle in handles)
            ShowWindowAsync(handle, command);
    }
}
'@

function Write-VisibilityResult {
    param([hashtable]$Value)

    $parent = Split-Path -Parent $ResultPath
    if (-not $parent -or -not (Test-Path -LiteralPath $parent -PathType Container)) {
        throw 'window_visibility_result_parent_missing'
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
        -not $requestItem.PSIsContainer -and
        -not ($requestItem.Attributes -band [IO.FileAttributes]::ReparsePoint)
    ) {
        $request = Get-Content -LiteralPath $RequestPath -Raw -Encoding UTF8 |
            ConvertFrom-Json
    }
    else {
        throw 'window_visibility_request_invalid'
    }

    if (
        $request.schema_version -ne 1 -or
        [int64]$request.process_id -le 0 -or
        [int64]$request.creation_time_unix_ms -le 0 -or
        [string]::IsNullOrWhiteSpace([string]$request.expected_executable) -or
        $request.action -notin @('hide', 'show')
    ) {
        throw 'window_visibility_request_invalid'
    }

    $process = Get-Process -Id ([int]$request.process_id) -ErrorAction Stop
    $observedExecutable = $process.Path
    $observedCreationTime = [DateTimeOffset]::new(
        $process.StartTime.ToUniversalTime()
    ).ToUnixTimeMilliseconds()
    if (
        -not [string]::Equals(
            $observedExecutable,
            [string]$request.expected_executable,
            [StringComparison]::OrdinalIgnoreCase
        ) -or
        $observedCreationTime -ne [int64]$request.creation_time_unix_ms
    ) {
        throw 'window_visibility_process_identity_mismatch'
    }

    $handles = @(
        [TradeJournalMt5WindowVisibility]::WindowsForProcess(
            [uint32]$request.process_id
        )
    )
    if ($handles.Count -eq 0) {
        throw 'window_visibility_window_not_found'
    }

    $visibleBefore = [TradeJournalMt5WindowVisibility]::VisibleCount($handles)
    $makeVisible = $request.action -eq 'show'
    [TradeJournalMt5WindowVisibility]::SetVisible($handles, $makeVisible)
    $deadline = (Get-Date).AddSeconds(5)
    do {
        Start-Sleep -Milliseconds 50
        $visibleAfter = [TradeJournalMt5WindowVisibility]::VisibleCount($handles)
        $complete = if ($makeVisible) {
            $visibleAfter -gt 0
        }
        else {
            $visibleAfter -eq 0
        }
    } while (-not $complete -and (Get-Date) -lt $deadline)

    if (-not $complete) {
        throw 'window_visibility_state_not_applied'
    }

    Write-VisibilityResult @{
        schema_version = 1
        success = $true
        action = [string]$request.action
        process_id = [int]$request.process_id
        creation_time_unix_ms = [int64]$request.creation_time_unix_ms
        windows_matched = $handles.Count
        visible_before = $visibleBefore
        visible_after = $visibleAfter
    }
    exit 0
}
catch {
    try {
        Write-VisibilityResult @{
            schema_version = 1
            success = $false
            action = 'unknown'
            process_id = 0
            creation_time_unix_ms = 0
            windows_matched = 0
            visible_before = 0
            visible_after = 0
            error_code = 'window_visibility_failed'
        }
    }
    catch {
        # The caller treats a missing result as a hard failure.
    }
    exit 1
}

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
    [DllImport("user32.dll")] static extern bool EnumWindows(EnumWindowProc cb, IntPtr param);
    [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
    [DllImport("user32.dll")] static extern IntPtr GetMenu(IntPtr h);
    [DllImport("user32.dll")] static extern IntPtr GetSubMenu(IntPtr h, int i);
    [DllImport("user32.dll")] static extern int GetMenuItemCount(IntPtr h);
    [DllImport("user32.dll")] static extern uint GetMenuItemID(IntPtr h, int i);
    [DllImport("user32.dll", CharSet=CharSet.Unicode)] static extern int GetMenuString(IntPtr h, uint i, System.Text.StringBuilder text, int size, uint flags);
    [DllImport("user32.dll", CharSet=CharSet.Unicode)] static extern int GetClassName(IntPtr h, System.Text.StringBuilder text, int size);
    [DllImport("user32.dll", CharSet=CharSet.Unicode, SetLastError=true)] static extern IntPtr SendMessageTimeout(IntPtr h, uint msg, UIntPtr w, IntPtr l, uint flags, uint timeout, out UIntPtr result);
    static string Label(IntPtr menu, int i) {
        var text = new System.Text.StringBuilder(256);
        GetMenuString(menu, (uint)i, text, text.Capacity, 0x400);
        return text.ToString().Replace("&", "").Trim();
    }
    static IntPtr Submenu(IntPtr menu, string label) {
        for(int i=0; i<GetMenuItemCount(menu); i++)
            if(Label(menu,i)==label) return GetSubMenu(menu,i);
        return IntPtr.Zero;
    }
    public static int Refresh(uint expectedPid) {
        var frames=new List<IntPtr>();
        EnumWindows(delegate(IntPtr h, IntPtr unused) {
            uint pid; GetWindowThreadProcessId(h,out pid);
            var name=new System.Text.StringBuilder(128);GetClassName(h,name,name.Capacity);
            if(pid==expectedPid && name.ToString()=="MetaQuotes::MetaTrader::5.00") frames.Add(h);
            return true;
        },IntPtr.Zero);
        if(frames.Count!=1) throw new Exception("managed_frame_ambiguous");
        var menu=GetMenu(frames[0]);
        var windows=Submenu(menu,"Window");
        var charts=Submenu(menu,"Charts");
        var periods=Submenu(charts,"Timeframes");
        if(windows==IntPtr.Zero || periods==IntPtr.Zero) throw new Exception("managed_menu_unavailable");
        string period=null;int count=0;
        for(int i=0;i<GetMenuItemCount(windows);i++) {
            uint id=GetMenuItemID(windows,i);
            if(id>=65280 && id<65344) {
                count++;
                var match=System.Text.RegularExpressions.Regex.Match(Label(windows,i),@",(M[0-9]+|H[0-9]+|D1|W1|MN1)$");
                if(match.Success)period=match.Groups[1].Value;
            }
        }
        if(count!=1 || period==null)throw new Exception("managed_chart_ambiguous");
        string requested=period=="M1"?"5 Minutes":"1 Minute";
        uint command=0;
        for(int i=0;i<GetMenuItemCount(periods);i++)
            if(Label(periods,i)==requested)command=GetMenuItemID(periods,i);
        if(command==0 || command==uint.MaxValue)throw new Exception("managed_period_unavailable");
        // Only the inspected timeframe command is dispatched. No trading/terminal command.
        UIntPtr result;
        if(SendMessageTimeout(frames[0],0x111,new UIntPtr(command),IntPtr.Zero,2,5000,out result)==IntPtr.Zero)
            throw new Exception("managed_chart_refresh_timeout");
        return frames.Count;
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
        $request.action -ne 'refresh_history'
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

    $matched = [TradeJournalMt5WindowVisibility]::Refresh([uint32]$request.process_id)

    Write-VisibilityResult @{
        schema_version = 1
        success = $true
        action = [string]$request.action
        process_id = [int]$request.process_id
        creation_time_unix_ms = [int64]$request.creation_time_unix_ms
        windows_matched = $matched
        visible_before = 0
        visible_after = 0
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

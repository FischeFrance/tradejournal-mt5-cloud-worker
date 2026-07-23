#requires -Version 5.1

<#
.SYNOPSIS
    Produce un inventario pre-start sanitizzato per i controlli C0-C5.

.DESCRIPTION
    PlanOnly e il default e non interroga il sistema. ReadOnlyChecks legge
    esclusivamente stato locale; non avvia MT5, non apre socket e non modifica
    firewall, registry o file. L'unica scrittura possibile e il nuovo JSON
    richiesto esplicitamente con -OutputPath.
#>
[CmdletBinding()]
param(
    [ValidateSet('PlanOnly', 'ReadOnlyChecks')]
    [string]$Mode = 'PlanOnly',

    [string]$PortableRoot = 'C:\TJLab\UNSET\terminal',

    [string]$PrivateDirectory = '',

    [ValidateRange(100, 500000)]
    [int]$MaxInventoryEntries = 100000,

    [string]$OutputPath = ''
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$script:CheckIds = @(
    'portable_storage_local_fixed',
    'portable_root_exists',
    'portable_inventory_complete',
    'reparse_points_absent',
    'accounts_dat_absent',
    'servers_dat_absent',
    'bases_absent',
    'profiles_account_artifacts_absent',
    'appdata_metaquotes_absent',
    'hkcu_metaquotes_absent',
    'terminal_metaeditor_processes_absent',
    'winhttp_proxy_direct',
    'wininet_proxy_direct',
    'credential_manager_cmdkey_scope_empty',
    'community_identity_known_markers_absent',
    'sensitive_bootstrap_absent'
)

function Get-LabSha256Bytes {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][byte[]]$Bytes)

    $sha256 = [Security.Cryptography.SHA256]::Create()
    try {
        return (($sha256.ComputeHash($Bytes) | ForEach-Object { $_.ToString('x2') }) -join '')
    }
    finally {
        $sha256.Dispose()
    }
}

function Get-LabSha256Text {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][AllowEmptyString()][string]$Text)

    return (Get-LabSha256Bytes -Bytes ([Text.Encoding]::UTF8.GetBytes($Text)))
}

function Get-LabSanitizedPathHash {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$Path)

    # Non risolve link, non accede al filesystem e non esporta il path.
    $normalized = $Path.Replace('/', '\').TrimEnd('\').ToLowerInvariant()
    return (Get-LabSha256Text -Text $normalized)
}

function New-LabPreStateCheck {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Id,
        [Parameter(Mandatory = $true)][ValidateSet('PASS', 'FAIL', 'UNKNOWN')][string]$Status,
        [AllowNull()][object]$Clean = $null,
        [AllowNull()][object]$Count = $null,
        [AllowNull()][object]$Digest = $null,
        [AllowNull()][object]$PathHash = $null,
        [Parameter(Mandatory = $true)][string]$ReasonCode
    )

    if ($script:CheckIds -cnotcontains $Id) {
        throw 'UNKNOWN_CHECK_ID'
    }
    if ($null -ne $Clean -and $Clean -isnot [bool]) {
        throw 'CHECK_CLEAN_MUST_BE_BOOLEAN_OR_NULL'
    }
    if ($null -ne $Count -and (($Count -isnot [int]) -and ($Count -isnot [long]) -or $Count -lt 0)) {
        throw 'CHECK_COUNT_MUST_BE_NON_NEGATIVE_OR_NULL'
    }
    if ($null -ne $Digest -and $Digest -cnotmatch '^[0-9a-f]{64}$') {
        throw 'CHECK_DIGEST_FORMAT_INVALID'
    }
    if ($null -ne $PathHash -and $PathHash -cnotmatch '^[0-9a-f]{64}$') {
        throw 'CHECK_PATH_HASH_FORMAT_INVALID'
    }
    if ($ReasonCode -cnotmatch '^[A-Z0-9_]{1,80}$') {
        throw 'CHECK_REASON_CODE_INVALID'
    }

    return [pscustomobject][ordered]@{
        id          = $Id
        status      = $Status
        clean       = $Clean
        count       = $Count
        digest      = $Digest
        path_hash   = $PathHash
        reason_code = $ReasonCode
    }
}

function Get-LabCheckById {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][object[]]$Checks,
        [Parameter(Mandatory = $true)][string]$Id
    )

    $match = @($Checks | Where-Object { $_.id -ceq $Id })
    if ($match.Count -ne 1) {
        throw 'CHECK_LOOKUP_NOT_UNIQUE'
    }
    return $match[0]
}

function Convert-LabCheckToNullableBoolean {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][psobject]$Check)

    if ($Check.status -ceq 'PASS') { return $true }
    if ($Check.status -ceq 'FAIL') { return $false }
    return $null
}

function Test-LabWindowsDrivePathSyntax {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$Path)

    if ([string]::IsNullOrWhiteSpace($Path)) { return $false }
    if ($Path -cmatch '^(\\|//|\\\?|\\\.)') { return $false }
    return ($Path -cmatch '^[A-Za-z]:[\\/](?![\\/])')
}

function Test-LabPathUnderDedicatedRoot {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$Path)

    if (-not (Test-LabWindowsDrivePathSyntax -Path $Path)) { return $false }
    try {
        $labRoot = [IO.Path]::GetFullPath('C:\TJLab').TrimEnd('\')
        $full = [IO.Path]::GetFullPath($Path).TrimEnd('\')
        return $full.StartsWith($labRoot + '\', [StringComparison]::OrdinalIgnoreCase)
    }
    catch {
        return $false
    }
}

function Get-LabLocalStorageAssessment {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$Path)

    $pathHash = Get-LabSanitizedPathHash -Path $Path
    if (-not (Test-LabWindowsDrivePathSyntax -Path $Path)) {
        return [pscustomobject]@{ Known = $true; IsLocalFixed = $false; PathHash = $pathHash; Reason = 'NON_LOCAL_OR_NON_ABSOLUTE_PATH' }
    }

    try {
        $driveRoot = $Path.Substring(0, 1).ToUpperInvariant() + ':\'
        $drive = New-Object IO.DriveInfo -ArgumentList $driveRoot
        if (-not $drive.IsReady) {
            return [pscustomobject]@{ Known = $true; IsLocalFixed = $false; PathHash = $pathHash; Reason = 'DRIVE_NOT_READY' }
        }
        if ($drive.DriveType -ne [IO.DriveType]::Fixed) {
            return [pscustomobject]@{ Known = $true; IsLocalFixed = $false; PathHash = $pathHash; Reason = 'DRIVE_NOT_FIXED' }
        }

        $providerDrive = Get-PSDrive -Name $Path.Substring(0, 1) -PSProvider FileSystem -ErrorAction Stop
        $displayRoot = $null
        if ($providerDrive.PSObject.Properties.Name -contains 'DisplayRoot') {
            $displayRoot = $providerDrive.DisplayRoot
        }
        if ($null -ne $displayRoot -and [string]$displayRoot -cmatch '^(\\|//)') {
            return [pscustomobject]@{ Known = $true; IsLocalFixed = $false; PathHash = $pathHash; Reason = 'MAPPED_NETWORK_DRIVE' }
        }

        return [pscustomobject]@{ Known = $true; IsLocalFixed = $true; PathHash = $pathHash; Reason = 'LOCAL_FIXED_DRIVE' }
    }
    catch {
        return [pscustomobject]@{ Known = $false; IsLocalFixed = $false; PathHash = $pathHash; Reason = 'DRIVE_CLASSIFICATION_FAILED' }
    }
}

function Get-LabPathChainAssessment {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$Path)

    try {
        $canonical = [IO.Path]::GetFullPath($Path).TrimEnd('\')
        $driveRoot = $canonical.Substring(0, 3)
        $relative = $canonical.Substring(3)
        $parts = @($relative.Split(@('\'), [StringSplitOptions]::RemoveEmptyEntries))
        $current = $driveRoot
        $reparseCount = 0
        $accessErrorCount = 0
        $existingComponents = 0

        foreach ($part in $parts) {
            $current = [IO.Path]::Combine($current, $part)
            try {
                if (-not (Test-Path -LiteralPath $current)) { break }
                $item = Get-Item -LiteralPath $current -Force -ErrorAction Stop
                $existingComponents++
                if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                    $reparseCount++
                    # Non seguire mai il target del reparse point.
                    break
                }
            }
            catch {
                $accessErrorCount++
                break
            }
        }

        return [pscustomobject]@{
            Complete           = ($accessErrorCount -eq 0)
            ReparseCount       = $reparseCount
            AccessErrorCount   = $accessErrorCount
            ExistingComponents = $existingComponents
            CanonicalPath      = $canonical
        }
    }
    catch {
        return [pscustomobject]@{
            Complete           = $false
            ReparseCount       = 0
            AccessErrorCount   = 1
            ExistingComponents = 0
            CanonicalPath      = $null
        }
    }
}

function Get-LabPortableInventory {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][int]$MaximumEntries
    )

    $entries = New-Object System.Collections.Generic.List[object]
    $pending = New-Object 'System.Collections.Generic.Stack[string]'
    $pending.Push($Root)
    $accessErrors = 0
    $truncated = $false

    while ($pending.Count -gt 0 -and -not $truncated) {
        $directory = $pending.Pop()
        try {
            $children = @(Get-ChildItem -LiteralPath $directory -Force -ErrorAction Stop)
        }
        catch {
            $accessErrors++
            continue
        }

        foreach ($child in $children) {
            if ($entries.Count -ge $MaximumEntries) {
                $truncated = $true
                break
            }

            try {
                $relative = $child.FullName.Substring($Root.Length).TrimStart('\')
                $isReparse = (($child.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0)
                $length = [long]0
                if (-not $child.PSIsContainer) { $length = [long]$child.Length }
                $entries.Add([pscustomobject]@{
                    RelativePath = $relative
                    IsDirectory  = [bool]$child.PSIsContainer
                    IsReparse    = [bool]$isReparse
                    Length       = $length
                })
                if ($child.PSIsContainer -and -not $isReparse) {
                    $pending.Push($child.FullName)
                }
            }
            catch {
                $accessErrors++
            }
        }
    }

    $records = @($entries | ForEach-Object {
        $relativeHash = Get-LabSha256Text -Text $_.RelativePath.Replace('/', '\').ToLowerInvariant()
        $kind = if ($_.IsDirectory) { 'D' } else { 'F' }
        $link = if ($_.IsReparse) { 'R' } else { 'N' }
        '{0}|{1}|{2}|{3}' -f $relativeHash, $kind, $_.Length, $link
    } | Sort-Object)
    $inventoryDigest = Get-LabSha256Text -Text ($records -join "`n")

    return [pscustomobject]@{
        Complete         = (($accessErrors -eq 0) -and -not $truncated)
        AccessErrorCount = $accessErrors
        Truncated        = $truncated
        Entries          = $entries
        EntryCount       = $entries.Count
        ReparseCount     = @($entries | Where-Object { $_.IsReparse }).Count
        Digest           = $inventoryDigest
    }
}

function New-LabInventoryAbsenceCheck {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Id,
        [Parameter(Mandatory = $true)][psobject]$Inventory,
        [Parameter(Mandatory = $true)][scriptblock]$Predicate
    )

    $matches = @($Inventory.Entries | Where-Object $Predicate)
    if ($matches.Count -gt 0) {
        $matchRecords = @($matches | ForEach-Object {
            Get-LabSha256Text -Text $_.RelativePath.Replace('/', '\').ToLowerInvariant()
        } | Sort-Object)
        return (New-LabPreStateCheck -Id $Id -Status FAIL -Clean $false -Count $matches.Count -Digest (Get-LabSha256Text -Text ($matchRecords -join "`n")) -ReasonCode 'RESIDUE_PRESENT')
    }
    if (-not $Inventory.Complete) {
        return (New-LabPreStateCheck -Id $Id -Status UNKNOWN -Count 0 -Digest $Inventory.Digest -ReasonCode 'INVENTORY_INCOMPLETE')
    }
    return (New-LabPreStateCheck -Id $Id -Status PASS -Clean $true -Count 0 -Digest $Inventory.Digest -ReasonCode 'RESIDUE_ABSENT')
}

function Get-LabAppDataAssessment {
    [CmdletBinding()]
    param()

    $roots = @(
        [Environment]::GetEnvironmentVariable('APPDATA', 'Process'),
        [Environment]::GetEnvironmentVariable('LOCALAPPDATA', 'Process')
    )
    if (@($roots | Where-Object { [string]::IsNullOrWhiteSpace($_) }).Count -gt 0) {
        return [pscustomobject]@{ Known = $false; PresentCount = 0; MarkerCount = 0; Digest = $null; Reason = 'APPDATA_ENVIRONMENT_UNAVAILABLE' }
    }

    $presentHashes = New-Object System.Collections.Generic.List[string]
    $communityMarkers = 0
    foreach ($root in $roots) {
        $storage = Get-LabLocalStorageAssessment -Path $root
        $chain = Get-LabPathChainAssessment -Path $root
        if (-not $storage.Known -or -not $storage.IsLocalFixed -or -not $chain.Complete -or $chain.ReparseCount -gt 0) {
            return [pscustomobject]@{ Known = $false; PresentCount = 0; MarkerCount = 0; Digest = $null; Reason = 'APPDATA_NOT_PROVABLY_LOCAL' }
        }

        try {
            # Enumerazione di un solo livello: individua la directory per nome
            # senza risolvere un eventuale target reparse MetaQuotes.
            $metaQuotesItems = @(Get-ChildItem -LiteralPath $root -Force -ErrorAction Stop | Where-Object { $_.Name -ieq 'MetaQuotes' })
            foreach ($metaQuotesItem in $metaQuotesItems) {
                $metaQuotes = $metaQuotesItem.FullName
                $presentHashes.Add((Get-LabSanitizedPathHash -Path $metaQuotes))
                # La sola root e gia un residuo. Non si entra al suo interno:
                # potrebbe essere essa stessa un reparse point verso rete.
            }
        }
        catch {
            return [pscustomobject]@{ Known = $false; PresentCount = $presentHashes.Count; MarkerCount = $communityMarkers; Digest = $null; Reason = 'APPDATA_READ_FAILED' }
        }
    }

    $records = @($presentHashes | Sort-Object)
    return [pscustomobject]@{
        Known        = $true
        PresentCount = $presentHashes.Count
        MarkerCount  = $communityMarkers
        Digest       = Get-LabSha256Text -Text ($records -join "`n")
        Reason       = if ($presentHashes.Count -eq 0) { 'METAQUOTES_APPDATA_ABSENT' } else { 'METAQUOTES_APPDATA_PRESENT' }
    }
}

function Get-LabMetaQuotesRegistryAssessment {
    [CmdletBinding()]
    param()

    $key = $null
    try {
        $key = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey('Software\MetaQuotes', $false)
        if ($null -eq $key) {
            return [pscustomobject]@{ Known = $true; PresentCount = 0; Digest = Get-LabSha256Text -Text ''; Reason = 'HKCU_METAQUOTES_ABSENT' }
        }
        # Non vengono letti o esportati nomi/value: la presenza della root basta
        # a falsificare il requisito di hive pulito.
        return [pscustomobject]@{ Known = $true; PresentCount = 1; Digest = Get-LabSha256Text -Text 'HKCU_METAQUOTES_ROOT_PRESENT'; Reason = 'HKCU_METAQUOTES_PRESENT' }
    }
    catch {
        return [pscustomobject]@{ Known = $false; PresentCount = 0; Digest = $null; Reason = 'HKCU_METAQUOTES_READ_FAILED' }
    }
    finally {
        if ($null -ne $key) { $key.Dispose() }
    }
}

function Get-LabProcessAssessment {
    [CmdletBinding()]
    param()

    try {
        $knownNames = @('terminal', 'terminal64', 'metaeditor', 'metaeditor64')
        $count = @(Get-Process -ErrorAction Stop | Where-Object { $knownNames -icontains $_.ProcessName }).Count
        return [pscustomobject]@{ Known = $true; Count = $count; Digest = Get-LabSha256Text -Text ('COUNT=' + $count) }
    }
    catch {
        return [pscustomobject]@{ Known = $false; Count = 0; Digest = $null }
    }
}

function Get-LabWinInetProxyAssessment {
    [CmdletBinding()]
    param()

    $internetSettings = $null
    $connections = $null
    try {
        $internetSettings = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey('Software\Microsoft\Windows\CurrentVersion\Internet Settings', $false)
        if ($null -eq $internetSettings) {
            return [pscustomobject]@{ Known = $true; IsDirect = $true; ResidueCount = 0; Digest = Get-LabSha256Text -Text 'WININET_DEFAULT_DIRECT' }
        }

        $proxyEnable = $internetSettings.GetValue('ProxyEnable', 0, [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
        $proxyServer = $internetSettings.GetValue('ProxyServer', $null, [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
        $autoConfigUrl = $internetSettings.GetValue('AutoConfigURL', $null, [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
        $proxyEnabled = ([int]$proxyEnable -ne 0)
        $serverResidue = ($null -ne $proxyServer -and -not [string]::IsNullOrWhiteSpace([string]$proxyServer))
        $pacConfigured = ($null -ne $autoConfigUrl -and -not [string]::IsNullOrWhiteSpace([string]$autoConfigUrl))
        $autoDetect = $false
        $malformed = $false

        $connections = $internetSettings.OpenSubKey('Connections', $false)
        if ($null -ne $connections) {
            $blob = $connections.GetValue('DefaultConnectionSettings', $null, [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
            if ($null -ne $blob) {
                if ($blob -is [byte[]] -and $blob.Length -ge 12) {
                    $flags = [BitConverter]::ToUInt32($blob, 8)
                    $proxyEnabled = $proxyEnabled -or (($flags -band 2) -ne 0)
                    $pacConfigured = $pacConfigured -or (($flags -band 4) -ne 0)
                    $autoDetect = (($flags -band 8) -ne 0)
                    if (($flags -band 0xfffffff0) -ne 0 -or ($flags -band 15) -eq 0) {
                        $malformed = $true
                    }
                }
                else {
                    $malformed = $true
                }
            }
        }

        if ($malformed) {
            return [pscustomobject]@{ Known = $false; IsDirect = $false; ResidueCount = 0; Digest = $null }
        }
        $residueCount = @(@($proxyEnabled, $serverResidue, $pacConfigured, $autoDetect) | Where-Object { $_ -eq $true }).Count
        $digestInput = 'enabled={0};serverResidue={1};pac={2};auto={3}' -f $proxyEnabled, $serverResidue, $pacConfigured, $autoDetect
        return [pscustomobject]@{
            Known        = $true
            IsDirect     = ($residueCount -eq 0)
            ResidueCount = $residueCount
            Digest       = Get-LabSha256Text -Text $digestInput
        }
    }
    catch {
        return [pscustomobject]@{ Known = $false; IsDirect = $false; ResidueCount = 0; Digest = $null }
    }
    finally {
        if ($null -ne $connections) { $connections.Dispose() }
        if ($null -ne $internetSettings) { $internetSettings.Dispose() }
    }
}

function Get-LabWinHttpProxyAssessment {
    [CmdletBinding()]
    param()

    $digests = New-Object System.Collections.Generic.List[string]
    $residueCount = 0
    $settingCount = 0
    $malformed = $false
    try {
        foreach ($view in @([Microsoft.Win32.RegistryView]::Registry64, [Microsoft.Win32.RegistryView]::Registry32)) {
            $baseKey = $null
            $connections = $null
            try {
                $baseKey = [Microsoft.Win32.RegistryKey]::OpenBaseKey([Microsoft.Win32.RegistryHive]::LocalMachine, $view)
                $connections = $baseKey.OpenSubKey('SOFTWARE\Microsoft\Windows\CurrentVersion\Internet Settings\Connections', $false)
                if ($null -eq $connections) { continue }
                $blob = $connections.GetValue('WinHttpSettings', $null, [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
                if ($null -eq $blob) { continue }
                $settingCount++
                if ($blob -isnot [byte[]] -or $blob.Length -lt 12) {
                    $malformed = $true
                    continue
                }
                $flags = [BitConverter]::ToUInt32($blob, 8)
                # Il digest copre soltanto i flag, mai le stringhe proxy/PAC
                # eventualmente presenti nel blob.
                $digests.Add((Get-LabSha256Text -Text ('FLAGS=' + $flags)))
                if (($flags -band 14) -ne 0) { $residueCount++ }
                if (($flags -band 1) -eq 0 -and ($flags -band 14) -eq 0) { $malformed = $true }
                if (($flags -band 0xfffffff0) -ne 0) { $malformed = $true }
            }
            finally {
                if ($null -ne $connections) { $connections.Dispose() }
                if ($null -ne $baseKey) { $baseKey.Dispose() }
            }
        }

        if ($malformed) {
            return [pscustomobject]@{ Known = $false; IsDirect = $false; ResidueCount = $residueCount; SettingCount = $settingCount; Digest = $null }
        }
        return [pscustomobject]@{
            Known        = $true
            IsDirect     = ($residueCount -eq 0)
            ResidueCount = $residueCount
            SettingCount = $settingCount
            Digest       = Get-LabSha256Text -Text (@($digests | Sort-Object) -join "`n")
        }
    }
    catch {
        return [pscustomobject]@{ Known = $false; IsDirect = $false; ResidueCount = 0; SettingCount = 0; Digest = $null }
    }
}

function Get-LabCmdKeyAssessment {
    [CmdletBinding()]
    param()

    $process = $null
    try {
        $systemRoot = [Environment]::GetEnvironmentVariable('SystemRoot', 'Process')
        if ([string]::IsNullOrWhiteSpace($systemRoot)) {
            return [pscustomobject]@{ Known = $false; Count = 0; CommunityCount = 0; Digest = $null; Reason = 'SYSTEM_ROOT_UNAVAILABLE' }
        }
        $cmdKeyPath = [IO.Path]::Combine($systemRoot, 'System32', 'cmdkey.exe')
        if (-not (Test-Path -LiteralPath $cmdKeyPath -PathType Leaf)) {
            return [pscustomobject]@{ Known = $false; Count = 0; CommunityCount = 0; Digest = $null; Reason = 'CMDKEY_UNAVAILABLE' }
        }
        $cmdKeyItem = Get-Item -LiteralPath $cmdKeyPath -Force
        if (($cmdKeyItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            return [pscustomobject]@{ Known = $false; Count = 0; CommunityCount = 0; Digest = $null; Reason = 'CMDKEY_REPARSE_REFUSED' }
        }
        $signature = Get-AuthenticodeSignature -LiteralPath $cmdKeyPath
        $subject = if ($null -ne $signature.SignerCertificate) { [string]$signature.SignerCertificate.Subject } else { '' }
        if ($signature.Status -ne 'Valid' -or $subject -notmatch 'Microsoft') {
            return [pscustomobject]@{ Known = $false; Count = 0; CommunityCount = 0; Digest = $null; Reason = 'CMDKEY_SIGNATURE_INVALID' }
        }

        $startInfo = New-Object Diagnostics.ProcessStartInfo
        $startInfo.FileName = $cmdKeyPath
        $startInfo.Arguments = '/list'
        $startInfo.UseShellExecute = $false
        $startInfo.CreateNoWindow = $true
        $startInfo.RedirectStandardOutput = $true
        $startInfo.RedirectStandardError = $true
        $process = New-Object Diagnostics.Process
        $process.StartInfo = $startInfo
        if (-not $process.Start()) {
            return [pscustomobject]@{ Known = $false; Count = 0; CommunityCount = 0; Digest = $null; Reason = 'CMDKEY_START_FAILED' }
        }
        if (-not $process.WaitForExit(10000)) {
            try { $process.Kill() } catch { }
            return [pscustomobject]@{ Known = $false; Count = 0; CommunityCount = 0; Digest = $null; Reason = 'CMDKEY_TIMEOUT' }
        }
        # cmdkey produce un output molto piccolo; la lettura avviene soltanto
        # dopo il timeout, cosi ReadToEnd non puo bloccare senza limite.
        $standardOutput = $process.StandardOutput.ReadToEnd()
        $standardError = $process.StandardError.ReadToEnd()
        if ($process.ExitCode -ne 0 -or -not [string]::IsNullOrWhiteSpace($standardError)) {
            return [pscustomobject]@{ Known = $false; Count = 0; CommunityCount = 0; Digest = $null; Reason = 'CMDKEY_FAILED' }
        }

        # Supporto intenzionalmente limitato e fail-closed. I nomi target sono
        # usati soltanto in memoria per i conteggi e non entrano nel report.
        $targetPattern = '(?im)^\s*(Target|Destinazione|Cible|Ziel|Destino|Doel|Alvo)\s*:\s*(.+?)\s*$'
        $targetMatches = @([regex]::Matches($standardOutput, $targetPattern))
        $emptyPattern = '(?im)^\s*\*?\s*(NONE|NESSUNA|AUCUNE|KEINE|NINGUNA|NENHUMA)\s*\*?\s*$'
        $emptyKnown = [regex]::IsMatch($standardOutput, $emptyPattern)
        $communityCount = 0
        foreach ($match in $targetMatches) {
            if ($match.Groups[2].Value -match '(?i)(MetaQuotes|MetaTrader|MQL5)') { $communityCount++ }
        }

        if ($targetMatches.Count -eq 0 -and -not $emptyKnown) {
            return [pscustomobject]@{ Known = $false; Count = 0; CommunityCount = 0; Digest = $null; Reason = 'CMDKEY_LOCALIZATION_UNSUPPORTED' }
        }
        $digestInput = 'exit=0;count={0};community={1};empty={2}' -f $targetMatches.Count, $communityCount, $emptyKnown
        return [pscustomobject]@{
            Known          = $true
            Count          = $targetMatches.Count
            CommunityCount = $communityCount
            Digest         = Get-LabSha256Text -Text $digestInput
            Reason         = if ($targetMatches.Count -eq 0) { 'CMDKEY_SCOPE_EMPTY' } else { 'CMDKEY_SCOPE_NOT_EMPTY' }
        }
    }
    catch {
        return [pscustomobject]@{ Known = $false; Count = 0; CommunityCount = 0; Digest = $null; Reason = 'CMDKEY_CHECK_FAILED' }
    }
    finally {
        if ($null -ne $process) { $process.Dispose() }
    }
}

function Get-LabPrivateBootstrapAssessment {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][AllowEmptyString()][string]$Path)

    if ([string]::IsNullOrWhiteSpace($Path)) {
        return [pscustomobject]@{ Known = $false; Present = $false; Count = 0; Digest = $null; PathHash = $null; Reason = 'PRIVATE_DIRECTORY_NOT_SUPPLIED' }
    }
    $storage = Get-LabLocalStorageAssessment -Path $Path
    if (-not $storage.Known -or -not $storage.IsLocalFixed) {
        return [pscustomobject]@{ Known = $false; Present = $false; Count = 0; Digest = $null; PathHash = $storage.PathHash; Reason = 'PRIVATE_DIRECTORY_NOT_LOCAL_FIXED' }
    }
    $chain = Get-LabPathChainAssessment -Path $Path
    if (-not $chain.Complete -or $chain.ReparseCount -gt 0) {
        return [pscustomobject]@{ Known = $false; Present = $false; Count = 0; Digest = $null; PathHash = $storage.PathHash; Reason = 'PRIVATE_DIRECTORY_CHAIN_UNSAFE' }
    }
    try {
        if (-not (Test-Path -LiteralPath $Path)) {
            return [pscustomobject]@{ Known = $true; Present = $false; Count = 0; Digest = Get-LabSha256Text -Text ''; PathHash = $storage.PathHash; Reason = 'PRIVATE_DIRECTORY_ABSENT' }
        }
        $item = Get-Item -LiteralPath $Path -Force
        if (-not $item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            return [pscustomobject]@{ Known = $true; Present = $true; Count = 1; Digest = Get-LabSha256Text -Text 'UNSAFE_PRIVATE_PATH_OBJECT'; PathHash = $storage.PathHash; Reason = 'PRIVATE_PATH_OBJECT_PRESENT' }
        }
        $children = @(Get-ChildItem -LiteralPath $Path -Force -ErrorAction Stop)
        $count = $children.Count
        return [pscustomobject]@{
            Known    = $true
            Present  = ($count -gt 0)
            Count    = $count
            Digest   = Get-LabSha256Text -Text ('PRIVATE_CHILD_COUNT=' + $count)
            PathHash = $storage.PathHash
            Reason   = if ($count -eq 0) { 'PRIVATE_DIRECTORY_EMPTY' } else { 'PRIVATE_DIRECTORY_NOT_EMPTY' }
        }
    }
    catch {
        return [pscustomobject]@{ Known = $false; Present = $false; Count = 0; Digest = $null; PathHash = $storage.PathHash; Reason = 'PRIVATE_DIRECTORY_READ_FAILED' }
    }
}

function New-LabEvidenceProjection {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][object[]]$Checks)

    $accounts = Convert-LabCheckToNullableBoolean (Get-LabCheckById -Checks $Checks -Id 'accounts_dat_absent')
    $servers = Convert-LabCheckToNullableBoolean (Get-LabCheckById -Checks $Checks -Id 'servers_dat_absent')
    $bases = Convert-LabCheckToNullableBoolean (Get-LabCheckById -Checks $Checks -Id 'bases_absent')
    $appData = Convert-LabCheckToNullableBoolean (Get-LabCheckById -Checks $Checks -Id 'appdata_metaquotes_absent')
    $registry = Convert-LabCheckToNullableBoolean (Get-LabCheckById -Checks $Checks -Id 'hkcu_metaquotes_absent')
    $bootstrap = Convert-LabCheckToNullableBoolean (Get-LabCheckById -Checks $Checks -Id 'sensitive_bootstrap_absent')
    $processes = Convert-LabCheckToNullableBoolean (Get-LabCheckById -Checks $Checks -Id 'terminal_metaeditor_processes_absent')
    $credentialCheck = Get-LabCheckById -Checks $Checks -Id 'credential_manager_cmdkey_scope_empty'
    $communityCheck = Get-LabCheckById -Checks $Checks -Id 'community_identity_known_markers_absent'

    # PASS scoped di cmdkey/marker noti non prova l'intero Credential Manager o
    # tutte le possibili identita Community. Un FAIL osservato invece falsifica
    # direttamente l'assenza.
    $credentialProjection = if ($credentialCheck.status -ceq 'FAIL') { $false } else { $null }
    $communityProjection = if ($communityCheck.status -ceq 'FAIL') { $false } else { $null }

    return [pscustomobject][ordered]@{
        portable_root_new          = $null
        disposable_clone_new       = $null
        windows_user_new           = $null
        accounts_dat_absent        = $accounts
        servers_dat_absent         = $servers
        bases_absent               = $bases
        appdata_absent             = $appData
        registry_clean             = $registry
        credential_manager_empty   = $credentialProjection
        community_identity_absent  = $communityProjection
        no_shared_storage          = $null
        sensitive_bootstrap_absent = $bootstrap
        prior_processes_absent     = $processes
        terminal_data_path_matches = $null
    }
}

function New-LabPreStateReport {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$SelectedMode,
        [Parameter(Mandatory = $true)][object[]]$Checks,
        [Parameter(Mandatory = $true)][bool]$OutputRequested
    )

    $failCount = @($Checks | Where-Object { $_.status -ceq 'FAIL' }).Count
    $unknownCount = @($Checks | Where-Object { $_.status -ceq 'UNKNOWN' }).Count
    $overall = if ($failCount -gt 0) { 'FAIL' } elseif ($unknownCount -gt 0) { 'UNKNOWN' } else { 'PASS' }
    $projection = New-LabEvidenceProjection -Checks $Checks
    $digestMaterial = [pscustomobject][ordered]@{
        mode                = $SelectedMode.ToUpperInvariant()
        checks              = $Checks
        evidence_projection = $projection
    }
    $reportDigest = Get-LabSha256Text -Text ($digestMaterial | ConvertTo-Json -Depth 8 -Compress)

    return [pscustomobject][ordered]@{
        schema_version               = 1
        mode                         = $SelectedMode.ToUpperInvariant()
        generated_at_utc             = [DateTime]::UtcNow.ToString('o')
        overall_status               = $overall
        check_count                  = $Checks.Count
        pass_count                   = @($Checks | Where-Object { $_.status -ceq 'PASS' }).Count
        fail_count                   = $failCount
        unknown_count                = $unknownCount
        checks                       = $Checks
        evidence_projection          = $projection
        operator_attestation_fields  = @(
            'portable_root_new',
            'disposable_clone_new',
            'windows_user_new',
            'credential_manager_empty',
            'community_identity_absent',
            'no_shared_storage',
            'terminal_data_path_matches'
        )
        privacy                      = [pscustomobject][ordered]@{
            raw_paths_exported             = $false
            registry_names_exported        = $false
            credential_names_exported      = $false
            proxy_values_exported          = $false
            sensitive_file_contents_read   = $false
            command_lines_exported         = $false
        }
        network_operations_performed = $false
        system_mutations_performed   = $false
        output_requested             = $OutputRequested
        output_written               = $false
        report_digest                = $reportDigest
    }
}

function Write-LabNewJsonFile {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Json
    )

    if (-not (Test-LabPathUnderDedicatedRoot -Path $Path)) { throw 'OUTPUT_PATH_MUST_BE_UNDER_C_TJLAB' }
    $storage = Get-LabLocalStorageAssessment -Path $Path
    if (-not $storage.Known -or -not $storage.IsLocalFixed) { throw 'OUTPUT_PATH_NOT_LOCAL_FIXED' }
    $parent = [IO.Path]::GetDirectoryName([IO.Path]::GetFullPath($Path))
    if ([string]::IsNullOrWhiteSpace($parent) -or -not (Test-Path -LiteralPath $parent -PathType Container)) {
        throw 'OUTPUT_PARENT_MUST_ALREADY_EXIST'
    }
    $chain = Get-LabPathChainAssessment -Path $parent
    if (-not $chain.Complete -or $chain.ReparseCount -gt 0) { throw 'OUTPUT_PARENT_CHAIN_UNSAFE' }
    $stream = $null
    $writer = $null
    try {
        $stream = [IO.File]::Open($Path, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
        $utf8NoBom = New-Object Text.UTF8Encoding($false)
        $writer = New-Object IO.StreamWriter($stream, $utf8NoBom)
        $writer.Write($Json)
        $writer.Flush()
        $stream.Flush($true)
    }
    catch {
        throw 'OUTPUT_WRITE_FAILED'
    }
    finally {
        if ($null -ne $writer) { $writer.Dispose() }
        elseif ($null -ne $stream) { $stream.Dispose() }
    }
}

function Get-LabPlanOnlyChecks {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$RootPath)

    $rootHash = Get-LabSanitizedPathHash -Path $RootPath
    $checks = New-Object System.Collections.Generic.List[object]
    foreach ($id in $script:CheckIds) {
        $pathHash = if ($id -in @('portable_storage_local_fixed', 'portable_root_exists', 'portable_inventory_complete', 'reparse_points_absent')) { $rootHash } else { $null }
        $checks.Add((New-LabPreStateCheck -Id $id -Status UNKNOWN -PathHash $pathHash -ReasonCode 'NOT_EXECUTED_PLAN_ONLY'))
    }
    return @($checks)
}

function Get-LabReadOnlyChecks {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$RootPath,
        [Parameter(Mandatory = $true)][AllowEmptyString()][string]$PrivatePath,
        [Parameter(Mandatory = $true)][int]$MaximumEntries
    )

    if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
        throw 'READ_ONLY_CHECKS_REQUIRE_WINDOWS'
    }
    if ([Environment]::Is64BitOperatingSystem -and -not [Environment]::Is64BitProcess) {
        throw 'READ_ONLY_CHECKS_REQUIRE_64_BIT_POWERSHELL'
    }
    if (-not (Test-LabPathUnderDedicatedRoot -Path $RootPath)) {
        throw 'PORTABLE_ROOT_MUST_BE_UNDER_C_TJLAB'
    }
    if (-not [string]::IsNullOrWhiteSpace($PrivatePath) -and
        -not (Test-LabPathUnderDedicatedRoot -Path $PrivatePath)) {
        throw 'PRIVATE_DIRECTORY_MUST_BE_UNDER_C_TJLAB'
    }

    $checks = New-Object System.Collections.Generic.List[object]
    $storage = Get-LabLocalStorageAssessment -Path $RootPath
    if (-not $storage.Known) {
        $checks.Add((New-LabPreStateCheck -Id 'portable_storage_local_fixed' -Status UNKNOWN -PathHash $storage.PathHash -ReasonCode $storage.Reason))
    }
    elseif (-not $storage.IsLocalFixed) {
        $checks.Add((New-LabPreStateCheck -Id 'portable_storage_local_fixed' -Status FAIL -Clean $false -Count 1 -PathHash $storage.PathHash -ReasonCode $storage.Reason))
    }
    else {
        $checks.Add((New-LabPreStateCheck -Id 'portable_storage_local_fixed' -Status PASS -Clean $true -Count 0 -PathHash $storage.PathHash -ReasonCode $storage.Reason))
    }

    $canInspectRoot = ($storage.Known -and $storage.IsLocalFixed)
    $chain = $null
    $rootExists = $false
    $inventory = $null
    if ($canInspectRoot) {
        $chain = Get-LabPathChainAssessment -Path $RootPath
        if (-not $chain.Complete) {
            $checks.Add((New-LabPreStateCheck -Id 'portable_root_exists' -Status UNKNOWN -PathHash $storage.PathHash -ReasonCode 'ROOT_CHAIN_READ_FAILED'))
        }
        elseif ($chain.ReparseCount -gt 0) {
            $checks.Add((New-LabPreStateCheck -Id 'portable_root_exists' -Status UNKNOWN -PathHash $storage.PathHash -ReasonCode 'ROOT_BEHIND_REPARSE_POINT'))
        }
        else {
            try { $rootExists = Test-Path -LiteralPath $RootPath -PathType Container } catch { $rootExists = $false; $canInspectRoot = $false }
            if ($rootExists) {
                $checks.Add((New-LabPreStateCheck -Id 'portable_root_exists' -Status PASS -Clean $true -Count 1 -PathHash $storage.PathHash -ReasonCode 'ROOT_DIRECTORY_PRESENT'))
                $canonicalRoot = [IO.Path]::GetFullPath($RootPath).TrimEnd('\')
                $inventory = Get-LabPortableInventory -Root $canonicalRoot -MaximumEntries $MaximumEntries
            }
            else {
                $checks.Add((New-LabPreStateCheck -Id 'portable_root_exists' -Status FAIL -Clean $false -Count 0 -PathHash $storage.PathHash -ReasonCode 'ROOT_DIRECTORY_ABSENT'))
            }
        }
    }
    else {
        $checks.Add((New-LabPreStateCheck -Id 'portable_root_exists' -Status UNKNOWN -PathHash $storage.PathHash -ReasonCode 'ROOT_NOT_INSPECTED_NON_LOCAL'))
    }

    if ($null -ne $inventory) {
        if ($inventory.Complete) {
            $checks.Add((New-LabPreStateCheck -Id 'portable_inventory_complete' -Status PASS -Clean $true -Count $inventory.EntryCount -Digest $inventory.Digest -PathHash $storage.PathHash -ReasonCode 'INVENTORY_COMPLETE'))
        }
        else {
            $checks.Add((New-LabPreStateCheck -Id 'portable_inventory_complete' -Status UNKNOWN -Count $inventory.EntryCount -Digest $inventory.Digest -PathHash $storage.PathHash -ReasonCode $(if ($inventory.Truncated) { 'INVENTORY_LIMIT_REACHED' } else { 'INVENTORY_ACCESS_ERRORS' })))
        }
        $totalReparse = $inventory.ReparseCount + $chain.ReparseCount
        if ($totalReparse -eq 0 -and $inventory.Complete) {
            $checks.Add((New-LabPreStateCheck -Id 'reparse_points_absent' -Status PASS -Clean $true -Count 0 -Digest $inventory.Digest -PathHash $storage.PathHash -ReasonCode 'NO_REPARSE_POINTS'))
        }
        elseif ($totalReparse -gt 0) {
            $checks.Add((New-LabPreStateCheck -Id 'reparse_points_absent' -Status FAIL -Clean $false -Count $totalReparse -Digest $inventory.Digest -PathHash $storage.PathHash -ReasonCode 'REPARSE_POINTS_PRESENT'))
        }
        else {
            $checks.Add((New-LabPreStateCheck -Id 'reparse_points_absent' -Status UNKNOWN -Count 0 -Digest $inventory.Digest -PathHash $storage.PathHash -ReasonCode 'REPARSE_SCAN_INCOMPLETE'))
        }

        $checks.Add((New-LabInventoryAbsenceCheck -Id 'accounts_dat_absent' -Inventory $inventory -Predicate {
            $_.RelativePath.Replace('/', '\') -ieq 'Config\accounts.dat'
        }))
        $checks.Add((New-LabInventoryAbsenceCheck -Id 'servers_dat_absent' -Inventory $inventory -Predicate {
            $_.RelativePath.Replace('/', '\') -ieq 'Config\servers.dat'
        }))
        $checks.Add((New-LabInventoryAbsenceCheck -Id 'bases_absent' -Inventory $inventory -Predicate {
            $relative = $_.RelativePath.Replace('/', '\')
            $relative -ieq 'Bases' -or $relative.StartsWith('Bases\', [StringComparison]::OrdinalIgnoreCase)
        }))
        $checks.Add((New-LabInventoryAbsenceCheck -Id 'profiles_account_artifacts_absent' -Inventory $inventory -Predicate {
            $relative = $_.RelativePath.Replace('/', '\')
            $relative -ieq 'Profiles' -or
                $relative.StartsWith('Profiles\', [StringComparison]::OrdinalIgnoreCase) -or
                $relative -ieq 'MQL5\Profiles' -or
                $relative.StartsWith('MQL5\Profiles\', [StringComparison]::OrdinalIgnoreCase)
        }))
    }
    else {
        $checks.Add((New-LabPreStateCheck -Id 'portable_inventory_complete' -Status UNKNOWN -PathHash $storage.PathHash -ReasonCode 'PORTABLE_ROOT_NOT_INVENTORIED'))
        if ($null -ne $chain -and $chain.ReparseCount -gt 0) {
            $checks.Add((New-LabPreStateCheck -Id 'reparse_points_absent' -Status FAIL -Clean $false -Count $chain.ReparseCount -PathHash $storage.PathHash -ReasonCode 'REPARSE_POINT_IN_ROOT_CHAIN'))
        }
        else {
            $checks.Add((New-LabPreStateCheck -Id 'reparse_points_absent' -Status UNKNOWN -PathHash $storage.PathHash -ReasonCode 'PORTABLE_ROOT_NOT_INVENTORIED'))
        }
        foreach ($id in @('accounts_dat_absent', 'servers_dat_absent', 'bases_absent', 'profiles_account_artifacts_absent')) {
            $checks.Add((New-LabPreStateCheck -Id $id -Status UNKNOWN -PathHash $storage.PathHash -ReasonCode 'PORTABLE_ROOT_NOT_INVENTORIED'))
        }
    }

    $appData = Get-LabAppDataAssessment
    if (-not $appData.Known) {
        $checks.Add((New-LabPreStateCheck -Id 'appdata_metaquotes_absent' -Status UNKNOWN -Count $appData.PresentCount -Digest $appData.Digest -ReasonCode $appData.Reason))
    }
    elseif ($appData.PresentCount -gt 0) {
        $checks.Add((New-LabPreStateCheck -Id 'appdata_metaquotes_absent' -Status FAIL -Clean $false -Count $appData.PresentCount -Digest $appData.Digest -ReasonCode $appData.Reason))
    }
    else {
        $checks.Add((New-LabPreStateCheck -Id 'appdata_metaquotes_absent' -Status PASS -Clean $true -Count 0 -Digest $appData.Digest -ReasonCode $appData.Reason))
    }

    $registry = Get-LabMetaQuotesRegistryAssessment
    if (-not $registry.Known) {
        $checks.Add((New-LabPreStateCheck -Id 'hkcu_metaquotes_absent' -Status UNKNOWN -Count $registry.PresentCount -Digest $registry.Digest -ReasonCode $registry.Reason))
    }
    elseif ($registry.PresentCount -gt 0) {
        $checks.Add((New-LabPreStateCheck -Id 'hkcu_metaquotes_absent' -Status FAIL -Clean $false -Count $registry.PresentCount -Digest $registry.Digest -ReasonCode $registry.Reason))
    }
    else {
        $checks.Add((New-LabPreStateCheck -Id 'hkcu_metaquotes_absent' -Status PASS -Clean $true -Count 0 -Digest $registry.Digest -ReasonCode $registry.Reason))
    }

    $processes = Get-LabProcessAssessment
    if (-not $processes.Known) {
        $checks.Add((New-LabPreStateCheck -Id 'terminal_metaeditor_processes_absent' -Status UNKNOWN -Count 0 -ReasonCode 'PROCESS_ENUMERATION_FAILED'))
    }
    elseif ($processes.Count -gt 0) {
        $checks.Add((New-LabPreStateCheck -Id 'terminal_metaeditor_processes_absent' -Status FAIL -Clean $false -Count $processes.Count -Digest $processes.Digest -ReasonCode 'TARGET_PROCESSES_PRESENT'))
    }
    else {
        $checks.Add((New-LabPreStateCheck -Id 'terminal_metaeditor_processes_absent' -Status PASS -Clean $true -Count 0 -Digest $processes.Digest -ReasonCode 'TARGET_PROCESSES_ABSENT'))
    }

    $winHttp = Get-LabWinHttpProxyAssessment
    if (-not $winHttp.Known) {
        $checks.Add((New-LabPreStateCheck -Id 'winhttp_proxy_direct' -Status UNKNOWN -Count $winHttp.ResidueCount -Digest $winHttp.Digest -ReasonCode 'WINHTTP_STATE_UNPARSABLE'))
    }
    elseif (-not $winHttp.IsDirect) {
        $checks.Add((New-LabPreStateCheck -Id 'winhttp_proxy_direct' -Status FAIL -Clean $false -Count $winHttp.ResidueCount -Digest $winHttp.Digest -ReasonCode 'WINHTTP_PROXY_OR_AUTO_CONFIGURED'))
    }
    else {
        $checks.Add((New-LabPreStateCheck -Id 'winhttp_proxy_direct' -Status PASS -Clean $true -Count 0 -Digest $winHttp.Digest -ReasonCode 'WINHTTP_DIRECT'))
    }

    $winInet = Get-LabWinInetProxyAssessment
    if (-not $winInet.Known) {
        $checks.Add((New-LabPreStateCheck -Id 'wininet_proxy_direct' -Status UNKNOWN -Count $winInet.ResidueCount -Digest $winInet.Digest -ReasonCode 'WININET_STATE_UNPARSABLE'))
    }
    elseif (-not $winInet.IsDirect) {
        $checks.Add((New-LabPreStateCheck -Id 'wininet_proxy_direct' -Status FAIL -Clean $false -Count $winInet.ResidueCount -Digest $winInet.Digest -ReasonCode 'WININET_PROXY_OR_AUTO_CONFIGURED'))
    }
    else {
        $checks.Add((New-LabPreStateCheck -Id 'wininet_proxy_direct' -Status PASS -Clean $true -Count 0 -Digest $winInet.Digest -ReasonCode 'WININET_DIRECT'))
    }

    $credentials = Get-LabCmdKeyAssessment
    if (-not $credentials.Known) {
        $checks.Add((New-LabPreStateCheck -Id 'credential_manager_cmdkey_scope_empty' -Status UNKNOWN -Count $credentials.Count -Digest $credentials.Digest -ReasonCode $credentials.Reason))
    }
    elseif ($credentials.Count -gt 0) {
        $checks.Add((New-LabPreStateCheck -Id 'credential_manager_cmdkey_scope_empty' -Status FAIL -Clean $false -Count $credentials.Count -Digest $credentials.Digest -ReasonCode $credentials.Reason))
    }
    else {
        $checks.Add((New-LabPreStateCheck -Id 'credential_manager_cmdkey_scope_empty' -Status PASS -Clean $true -Count 0 -Digest $credentials.Digest -ReasonCode $credentials.Reason))
    }

    $knownCommunityMarkers = $appData.MarkerCount + $credentials.CommunityCount
    if ($knownCommunityMarkers -gt 0) {
        $checks.Add((New-LabPreStateCheck -Id 'community_identity_known_markers_absent' -Status FAIL -Clean $false -Count $knownCommunityMarkers -ReasonCode 'KNOWN_COMMUNITY_MARKERS_PRESENT'))
    }
    elseif (-not $appData.Known -or $appData.PresentCount -gt 0 -or -not $credentials.Known -or -not $registry.Known -or $registry.PresentCount -gt 0) {
        $checks.Add((New-LabPreStateCheck -Id 'community_identity_known_markers_absent' -Status UNKNOWN -Count 0 -ReasonCode 'COMMUNITY_SCOPE_INCOMPLETE'))
    }
    else {
        $checks.Add((New-LabPreStateCheck -Id 'community_identity_known_markers_absent' -Status PASS -Clean $true -Count 0 -ReasonCode 'KNOWN_COMMUNITY_MARKERS_ABSENT'))
    }

    $private = Get-LabPrivateBootstrapAssessment -Path $PrivatePath
    $expectedAccountInputCount = 0
    $portableInputKnown = ($null -ne $inventory -and $inventory.Complete)
    if ($null -ne $inventory) {
        $expectedAccountInputCount = @($inventory.Entries | Where-Object {
            $_.RelativePath.Replace('/', '\') -ieq 'MQL5\Files\MT5DirectEndpointLab\expected-account.txt'
        }).Count
    }
    $sensitiveResidueCount = $private.Count + $expectedAccountInputCount
    $sensitiveDigest = Get-LabSha256Text -Text ('private={0};expectedInput={1};portableDigest={2}' -f $private.Count, $expectedAccountInputCount, $(if ($null -ne $inventory) { $inventory.Digest } else { 'UNKNOWN' }))
    if ($private.Present -or $expectedAccountInputCount -gt 0) {
        $checks.Add((New-LabPreStateCheck -Id 'sensitive_bootstrap_absent' -Status FAIL -Clean $false -Count $sensitiveResidueCount -Digest $sensitiveDigest -PathHash $private.PathHash -ReasonCode 'SENSITIVE_BOOTSTRAP_RESIDUE_PRESENT'))
    }
    elseif (-not $private.Known -or -not $portableInputKnown) {
        $checks.Add((New-LabPreStateCheck -Id 'sensitive_bootstrap_absent' -Status UNKNOWN -Count $sensitiveResidueCount -Digest $sensitiveDigest -PathHash $private.PathHash -ReasonCode 'SENSITIVE_BOOTSTRAP_SCOPE_INCOMPLETE'))
    }
    else {
        $checks.Add((New-LabPreStateCheck -Id 'sensitive_bootstrap_absent' -Status PASS -Clean $true -Count 0 -Digest $sensitiveDigest -PathHash $private.PathHash -ReasonCode 'SENSITIVE_BOOTSTRAP_ABSENT'))
    }

    if ($checks.Count -ne $script:CheckIds.Count) { throw 'INTERNAL_CHECK_COUNT_MISMATCH' }
    foreach ($id in $script:CheckIds) {
        [void](Get-LabCheckById -Checks @($checks) -Id $id)
    }
    return @($checks)
}

$selectedChecks = if ($Mode -ceq 'PlanOnly') {
    Get-LabPlanOnlyChecks -RootPath $PortableRoot
}
else {
    Get-LabReadOnlyChecks -RootPath $PortableRoot -PrivatePath $PrivateDirectory -MaximumEntries $MaxInventoryEntries
}

$report = New-LabPreStateReport -SelectedMode $Mode -Checks @($selectedChecks) -OutputRequested (-not [string]::IsNullOrWhiteSpace($OutputPath))
if (-not [string]::IsNullOrWhiteSpace($OutputPath)) {
    $report.output_written = $true
}
$json = $report | ConvertTo-Json -Depth 10
if (-not [string]::IsNullOrWhiteSpace($OutputPath)) {
    Write-LabNewJsonFile -Path $OutputPath -Json $json
}
[Console]::Out.WriteLine($json)

#requires -Version 5.1

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Test-LabRunId {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$RunId
    )

    return ($RunId -cmatch '^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$')
}

function Assert-LabRunId {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$RunId
    )

    if (-not (Test-LabRunId -RunId $RunId)) {
        throw "RunId non valido. Usare 1-64 caratteri ASCII: lettere, numeri, '_' o '-'."
    }
}

function Join-LabPath {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$Parent,

        [Parameter(Mandatory = $true)]
        [string]$Child
    )

    # Consente di generare piani Windows anche durante un dry-run PowerShell su
    # Linux/macOS, dove il provider FileSystem non espone il drive C:.
    if ($Parent -cmatch '^[A-Za-z]:\\') {
        return ($Parent.TrimEnd('\') + '\' + $Child.TrimStart('\'))
    }
    return [IO.Path]::Combine($Parent, $Child)
}

function Get-LabTrustedSystemToolPath {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [ValidateSet('wpr.exe', 'tracerpt.exe', 'netsh.exe', 'auditpol.exe')]
        [string]$Name
    )

    if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
        throw "$Name richiede Windows."
    }
    if ([Environment]::Is64BitOperatingSystem -and -not [Environment]::Is64BitProcess) {
        throw "$Name richiede PowerShell a 64 bit per evitare la redirezione System32."
    }
    $systemRoot = [Environment]::GetEnvironmentVariable('SystemRoot')
    if ([string]::IsNullOrWhiteSpace($systemRoot)) {
        throw 'SystemRoot non disponibile.'
    }
    $expectedPath = [IO.Path]::Combine($systemRoot, 'System32', $Name)
    if (-not (Test-Path -LiteralPath $expectedPath -PathType Leaf)) {
        throw "Tool di sistema non trovato nel percorso atteso: $expectedPath"
    }
    $item = Get-Item -LiteralPath $expectedPath -Force
    if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "Tool di sistema su reparse point rifiutato: $expectedPath"
    }
    $signature = Get-AuthenticodeSignature -LiteralPath $expectedPath
    $subject = if ($null -ne $signature.SignerCertificate) { [string]$signature.SignerCertificate.Subject } else { '' }
    if ($signature.Status -ne 'Valid' -or $subject -notmatch 'Microsoft') {
        throw "Firma Authenticode Microsoft non valida per $expectedPath. Stato: $($signature.Status)"
    }
    return $item.FullName
}

function Test-LabIpInCidr {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [System.Net.IPAddress]$Address,

        [Parameter(Mandatory = $true)]
        [string]$Cidr
    )

    $parts = $Cidr.Split('/')
    if ($parts.Count -ne 2) {
        throw "CIDR non valido: $Cidr"
    }

    $network = $null
    if (-not [System.Net.IPAddress]::TryParse($parts[0], [ref]$network)) {
        throw "Indirizzo di rete CIDR non valido: $Cidr"
    }

    $prefixLength = 0
    if (-not [int]::TryParse($parts[1], [ref]$prefixLength)) {
        throw "Prefisso CIDR non valido: $Cidr"
    }

    $addressBytes = $Address.GetAddressBytes()
    $networkBytes = $network.GetAddressBytes()
    if ($addressBytes.Length -ne $networkBytes.Length) {
        return $false
    }

    $maxPrefix = $addressBytes.Length * 8
    if ($prefixLength -lt 0 -or $prefixLength -gt $maxPrefix) {
        throw "Prefisso CIDR fuori intervallo: $Cidr"
    }

    $wholeBytes = [Math]::Floor($prefixLength / 8)
    for ($index = 0; $index -lt $wholeBytes; $index++) {
        if ($addressBytes[$index] -ne $networkBytes[$index]) {
            return $false
        }
    }

    $remainingBits = $prefixLength % 8
    if ($remainingBits -gt 0) {
        $mask = (0xFF -shl (8 - $remainingBits)) -band 0xFF
        if (($addressBytes[$wholeBytes] -band $mask) -ne ($networkBytes[$wholeBytes] -band $mask)) {
            return $false
        }
    }

    return $true
}

function Test-LabLiteralAddressSafety {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$Address
    )

    $reasons = New-Object System.Collections.Generic.List[string]
    $parsed = $null
    if (-not [System.Net.IPAddress]::TryParse($Address, [ref]$parsed)) {
        $reasons.Add('INVALID_IP_LITERAL')
        return [pscustomobject]@{
            Address       = $Address
            Normalized    = $null
            AddressFamily = $null
            IsPublic      = $false
            Reasons       = @($reasons)
        }
    }

    if ($parsed.AddressFamily -eq [System.Net.Sockets.AddressFamily]::InterNetworkV6 -and
        $parsed.IsIPv4MappedToIPv6) {
        $mapped = $parsed.MapToIPv4()
        $mappedResult = Test-LabLiteralAddressSafety -Address $mapped.ToString()
        $mappedReasons = New-Object System.Collections.Generic.List[string]
        $mappedReasons.Add('IPV4_MAPPED_IPV6_NOT_ALLOWED')
        foreach ($reason in $mappedResult.Reasons) {
            $mappedReasons.Add($reason)
        }
        return [pscustomobject]@{
            Address       = $Address
            Normalized    = $parsed.ToString().ToLowerInvariant()
            AddressFamily = 'IPv6'
            IsPublic      = $false
            Reasons       = @($mappedReasons | Select-Object -Unique)
        }
    }

    $blockedCidrs = @()
    if ($parsed.AddressFamily -eq [System.Net.Sockets.AddressFamily]::InterNetwork) {
        $blockedCidrs = @(
            [pscustomobject]@{ Cidr = '0.0.0.0/8'; Reason = 'IPV4_THIS_NETWORK' },
            [pscustomobject]@{ Cidr = '10.0.0.0/8'; Reason = 'IPV4_PRIVATE' },
            [pscustomobject]@{ Cidr = '100.64.0.0/10'; Reason = 'IPV4_SHARED_CGNAT' },
            [pscustomobject]@{ Cidr = '127.0.0.0/8'; Reason = 'IPV4_LOOPBACK' },
            [pscustomobject]@{ Cidr = '169.254.0.0/16'; Reason = 'IPV4_LINK_LOCAL' },
            [pscustomobject]@{ Cidr = '172.16.0.0/12'; Reason = 'IPV4_PRIVATE' },
            [pscustomobject]@{ Cidr = '192.0.0.0/24'; Reason = 'IPV4_IETF_PROTOCOL_ASSIGNMENT' },
            [pscustomobject]@{ Cidr = '192.0.2.0/24'; Reason = 'IPV4_DOCUMENTATION' },
            [pscustomobject]@{ Cidr = '192.31.196.0/24'; Reason = 'IPV4_SPECIAL_SERVICE' },
            [pscustomobject]@{ Cidr = '192.52.193.0/24'; Reason = 'IPV4_SPECIAL_SERVICE' },
            [pscustomobject]@{ Cidr = '192.88.99.0/24'; Reason = 'IPV4_RELAY_RESERVED' },
            [pscustomobject]@{ Cidr = '192.168.0.0/16'; Reason = 'IPV4_PRIVATE' },
            [pscustomobject]@{ Cidr = '192.175.48.0/24'; Reason = 'IPV4_SPECIAL_SERVICE' },
            [pscustomobject]@{ Cidr = '198.18.0.0/15'; Reason = 'IPV4_BENCHMARK' },
            [pscustomobject]@{ Cidr = '198.51.100.0/24'; Reason = 'IPV4_DOCUMENTATION' },
            [pscustomobject]@{ Cidr = '203.0.113.0/24'; Reason = 'IPV4_DOCUMENTATION' },
            [pscustomobject]@{ Cidr = '224.0.0.0/4'; Reason = 'IPV4_MULTICAST' },
            [pscustomobject]@{ Cidr = '240.0.0.0/4'; Reason = 'IPV4_RESERVED' }
        )
    }
    elseif ($parsed.AddressFamily -eq [System.Net.Sockets.AddressFamily]::InterNetworkV6) {
        $blockedCidrs = @(
            [pscustomobject]@{ Cidr = '::/96'; Reason = 'IPV6_IPV4_COMPATIBLE_OR_UNSPECIFIED' },
            [pscustomobject]@{ Cidr = '64:ff9b::/96'; Reason = 'IPV6_NAT64_WELL_KNOWN' },
            [pscustomobject]@{ Cidr = '64:ff9b:1::/48'; Reason = 'IPV6_NAT64_LOCAL_USE' },
            [pscustomobject]@{ Cidr = '100::/64'; Reason = 'IPV6_DISCARD_ONLY' },
            [pscustomobject]@{ Cidr = '2001::/23'; Reason = 'IPV6_IETF_SPECIAL_PURPOSE' },
            [pscustomobject]@{ Cidr = '2001:db8::/32'; Reason = 'IPV6_DOCUMENTATION' },
            [pscustomobject]@{ Cidr = '2002::/16'; Reason = 'IPV6_6TO4' },
            [pscustomobject]@{ Cidr = '3fff::/20'; Reason = 'IPV6_DOCUMENTATION' },
            [pscustomobject]@{ Cidr = '5f00::/16'; Reason = 'IPV6_SEGMENT_ROUTING_SPECIAL' },
            [pscustomobject]@{ Cidr = 'fc00::/7'; Reason = 'IPV6_UNIQUE_LOCAL' },
            [pscustomobject]@{ Cidr = 'fe80::/10'; Reason = 'IPV6_LINK_LOCAL' },
            [pscustomobject]@{ Cidr = 'ff00::/8'; Reason = 'IPV6_MULTICAST' }
        )
    }
    else {
        $reasons.Add('UNSUPPORTED_ADDRESS_FAMILY')
    }

    foreach ($blocked in $blockedCidrs) {
        if (Test-LabIpInCidr -Address $parsed -Cidr $blocked.Cidr) {
            $reasons.Add([string]$blocked.Reason)
        }
    }

    if ($parsed.AddressFamily -eq [System.Net.Sockets.AddressFamily]::InterNetworkV6 -and
        -not (Test-LabIpInCidr -Address $parsed -Cidr '2000::/3')) {
        $reasons.Add('IPV6_NOT_GLOBAL_UNICAST')
    }

    return [pscustomobject]@{
        Address       = $Address
        Normalized    = $parsed.ToString().ToLowerInvariant()
        AddressFamily = if ($parsed.AddressFamily -eq [System.Net.Sockets.AddressFamily]::InterNetwork) { 'IPv4' } else { 'IPv6' }
        IsPublic      = ($reasons.Count -eq 0)
        Reasons       = @($reasons | Select-Object -Unique)
    }
}

function Test-LabDnsNameSyntax {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$HostName
    )

    $reasons = New-Object System.Collections.Generic.List[string]
    $normalized = $HostName.TrimEnd('.').ToLowerInvariant()

    if ($HostName.EndsWith('..', [System.StringComparison]::Ordinal)) {
        $reasons.Add('DNS_MULTIPLE_TRAILING_DOTS')
    }
    if ($HostName -cnotmatch '^[\x00-\x7F]+$') {
        $reasons.Add('DNS_NON_ASCII_NOT_ALLOWED')
    }
    if ($normalized.Length -lt 1 -or $normalized.Length -gt 253) {
        $reasons.Add('DNS_LENGTH_INVALID')
    }
    if ($normalized.IndexOf('.') -lt 1) {
        $reasons.Add('DNS_SINGLE_LABEL_NOT_ALLOWED')
    }
    if ($normalized -match '[*_%\\/@?#:\s]') {
        $reasons.Add('DNS_FORBIDDEN_CHARACTER')
    }

    foreach ($label in $normalized.Split('.')) {
        if ($label.Length -lt 1 -or $label.Length -gt 63 -or
            $label -cnotmatch '^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$') {
            $reasons.Add('DNS_LABEL_INVALID')
            break
        }
    }

    $reservedSuffixes = @(
        'localhost', '.localhost', '.local', '.lan', '.home', '.internal',
        '.test', '.invalid', '.example', '.example.com', '.example.net',
        '.example.org', '.onion', '.alt', '.arpa'
    )
    foreach ($suffix in $reservedSuffixes) {
        if ($suffix.StartsWith('.')) {
            if ($normalized.EndsWith($suffix, [System.StringComparison]::OrdinalIgnoreCase)) {
                $reasons.Add('DNS_RESERVED_OR_LOCAL_SUFFIX')
                break
            }
        }
        elseif ($normalized.Equals($suffix, [System.StringComparison]::OrdinalIgnoreCase)) {
            $reasons.Add('DNS_RESERVED_OR_LOCAL_SUFFIX')
            break
        }
    }

    if ($normalized -match '^[0-9.]+$') {
        $reasons.Add('DNS_AMBIGUOUS_NUMERIC_NAME')
    }

    return [pscustomobject]@{
        HostName   = $HostName
        Normalized = $normalized
        IsValid    = ($reasons.Count -eq 0)
        Reasons    = @($reasons | Select-Object -Unique)
    }
}

function Test-LabEndpoint {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$Endpoint,

        [int[]]$AllowedPort = @(),

        [string[]]$ResolvedAddress = @()
    )

    $reasons = New-Object System.Collections.Generic.List[string]
    $hostValue = $null
    $portText = $null
    $bracketed = $false

    if ($Endpoint -cmatch '^\[(?<host>[^\]]+)\]:(?<port>[0-9]{1,5})$') {
        $hostValue = $Matches.host
        $portText = $Matches.port
        $bracketed = $true
    }
    elseif ($Endpoint -cmatch '^(?<host>[^:\s/\\?#@]+):(?<port>[0-9]{1,5})$') {
        $hostValue = $Matches.host
        $portText = $Matches.port
    }
    else {
        $reasons.Add('ENDPOINT_FORMAT_INVALID')
    }

    $port = 0
    if ($null -ne $portText) {
        if (-not [int]::TryParse($portText, [ref]$port) -or $port -lt 1 -or $port -gt 65535) {
            $reasons.Add('PORT_OUT_OF_RANGE')
        }
        elseif ($portText -cne [string]$port) {
            $reasons.Add('NON_CANONICAL_PORT')
        }
    }

    $portApproved = $false
    if ($port -ge 1 -and $port -le 65535) {
        $portApproved = ($AllowedPort -contains $port)
        if (-not $portApproved) {
            $reasons.Add('PORT_NOT_EXPLICITLY_APPROVED')
        }
    }

    $hostKind = $null
    $normalizedHost = $null
    $addressEvidence = New-Object System.Collections.Generic.List[object]
    $syntaxValid = ($reasons -notcontains 'ENDPOINT_FORMAT_INVALID' -and
        $reasons -notcontains 'PORT_OUT_OF_RANGE' -and
        $reasons -notcontains 'NON_CANONICAL_PORT')

    if ($null -ne $hostValue) {
        $parsedHost = $null
        if ([System.Net.IPAddress]::TryParse($hostValue, [ref]$parsedHost)) {
            $hostKind = if ($parsedHost.AddressFamily -eq [System.Net.Sockets.AddressFamily]::InterNetwork) { 'IPv4' } else { 'IPv6' }
            $normalizedHost = $parsedHost.ToString().ToLowerInvariant()

            if ($hostKind -eq 'IPv6' -and -not $bracketed) {
                $reasons.Add('IPV6_BRACKETS_REQUIRED')
                $syntaxValid = $false
            }
            if ($hostKind -eq 'IPv4' -and $bracketed) {
                $reasons.Add('IPV4_BRACKETS_NOT_ALLOWED')
                $syntaxValid = $false
            }
            if ($hostKind -eq 'IPv4' -and $hostValue -cne $normalizedHost) {
                $reasons.Add('NON_CANONICAL_IPV4_LITERAL')
                $syntaxValid = $false
            }
            if ($hostValue.Contains('%')) {
                $reasons.Add('IPV6_ZONE_ID_NOT_ALLOWED')
                $syntaxValid = $false
            }

            $literalSafety = Test-LabLiteralAddressSafety -Address $normalizedHost
            $addressEvidence.Add($literalSafety)
            foreach ($reason in $literalSafety.Reasons) {
                $reasons.Add([string]$reason)
            }
        }
        else {
            $hostKind = 'DNS'
            $dnsSyntax = Test-LabDnsNameSyntax -HostName $hostValue
            $normalizedHost = $dnsSyntax.Normalized
            if (-not $dnsSyntax.IsValid) {
                $syntaxValid = $false
                foreach ($reason in $dnsSyntax.Reasons) {
                    $reasons.Add([string]$reason)
                }
            }

            if ($ResolvedAddress.Count -eq 0) {
                $reasons.Add('DNS_RRSET_EVIDENCE_REQUIRED')
            }
            else {
                foreach ($candidateAddress in ($ResolvedAddress | Select-Object -Unique)) {
                    $addressResult = Test-LabLiteralAddressSafety -Address $candidateAddress
                    $addressEvidence.Add($addressResult)
                    foreach ($reason in $addressResult.Reasons) {
                        $reasons.Add([string]$reason)
                    }
                }
            }
        }
    }

    $unsafeReasons = @($reasons | Where-Object {
        $_ -notin @('PORT_NOT_EXPLICITLY_APPROVED', 'DNS_RRSET_EVIDENCE_REQUIRED')
    })
    $status = 'UNSAFE'
    if ($syntaxValid -and $unsafeReasons.Count -eq 0) {
        if (-not $portApproved) {
            $status = 'REQUIRES_PORT_APPROVAL'
        }
        elseif ($hostKind -eq 'DNS' -and $ResolvedAddress.Count -eq 0) {
            $status = 'REQUIRES_DNS_EVIDENCE'
        }
        else {
            $status = 'SAFE'
        }
    }

    $normalizedEndpoint = $null
    if ($null -ne $normalizedHost -and $port -gt 0) {
        if ($hostKind -eq 'IPv6') {
            $normalizedEndpoint = "[$normalizedHost]:$port"
        }
        else {
            $normalizedEndpoint = "${normalizedHost}:$port"
        }
    }

    return [pscustomobject]@{
        SchemaVersion      = 1
        InputEndpoint      = $Endpoint
        NormalizedEndpoint = $normalizedEndpoint
        HostKind           = $hostKind
        NormalizedHost     = $normalizedHost
        Port               = $port
        SyntaxValid        = $syntaxValid
        PortApproved       = $portApproved
        SafetyStatus       = $status
        ServingEligible    = ($status -eq 'SAFE')
        AddressEvidence    = @($addressEvidence.ToArray())
        Reasons            = @($reasons | Select-Object -Unique)
    }
}

function ConvertTo-LabCommandPreview {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$Executable,

        [Parameter(Mandatory = $true)]
        [string[]]$ArgumentList
    )

    $parts = New-Object System.Collections.Generic.List[string]
    $parts.Add(('"{0}"' -f $Executable.Replace('"', '""')))
    foreach ($argument in $ArgumentList) {
        if ($argument -match '[\s"]') {
            $parts.Add(('"{0}"' -f $argument.Replace('"', '""')))
        }
        else {
            $parts.Add($argument)
        }
    }
    return ($parts -join ' ')
}

function Get-LabAuditPolicyFlags {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [Guid]$SubcategoryGuid
    )

    if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
        throw 'AuditQuerySystemPolicy richiede Windows.'
    }

    # auditpol /get /r produce testo localizzato. La API nativa restituisce
    # invece direttamente il bitmask: SUCCESS=1, FAILURE=2. La funzione viene
    # caricata soltanto nelle modalita Windows attive, mai durante PlanOnly.
    if ($null -eq ('MT5Lab.NativeAuditPolicy' -as [type])) {
        Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;

namespace MT5Lab
{
    public static class NativeAuditPolicy
    {
        [StructLayout(LayoutKind.Sequential)]
        private struct AUDIT_POLICY_INFORMATION
        {
            public Guid AuditSubCategoryGuid;
            public uint AuditingInformation;
            public Guid AuditCategoryGuid;
        }

        [DllImport("advapi32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.U1)]
        private static extern bool AuditQuerySystemPolicy(
            [In] Guid[] pSubCategoryGuids,
            uint policyCount,
            out IntPtr ppAuditPolicy);

        [DllImport("advapi32.dll")]
        private static extern void AuditFree(IntPtr buffer);

        public static uint Query(Guid subcategoryGuid)
        {
            IntPtr buffer = IntPtr.Zero;
            if (!AuditQuerySystemPolicy(new[] { subcategoryGuid }, 1, out buffer))
            {
                throw new Win32Exception(Marshal.GetLastWin32Error(),
                    "AuditQuerySystemPolicy failed");
            }

            try
            {
                if (buffer == IntPtr.Zero)
                {
                    throw new InvalidOperationException(
                        "AuditQuerySystemPolicy returned a null buffer");
                }

                // ppAuditPolicy riceve direttamente PAUDIT_POLICY_INFORMATION.
                // Con policyCount=1 il puntatore restituito indirizza la prima
                // struttura; non va dereferenziato come un ulteriore IntPtr.
                AUDIT_POLICY_INFORMATION information =
                    (AUDIT_POLICY_INFORMATION)Marshal.PtrToStructure(
                        buffer, typeof(AUDIT_POLICY_INFORMATION));
                if (information.AuditSubCategoryGuid != subcategoryGuid)
                {
                    throw new InvalidOperationException(
                        "AuditQuerySystemPolicy returned a different subcategory");
                }
                return information.AuditingInformation;
            }
            finally
            {
                if (buffer != IntPtr.Zero)
                {
                    AuditFree(buffer);
                }
            }
        }
    }
}
'@
    }

    return [uint32][MT5Lab.NativeAuditPolicy]::Query($SubcategoryGuid)
}

Export-ModuleMember -Function @(
    'Assert-LabRunId',
    'ConvertTo-LabCommandPreview',
    'Get-LabAuditPolicyFlags',
    'Get-LabTrustedSystemToolPath',
    'Join-LabPath',
    'Test-LabDnsNameSyntax',
    'Test-LabEndpoint',
    'Test-LabIpInCidr',
    'Test-LabLiteralAddressSafety',
    'Test-LabRunId'
)

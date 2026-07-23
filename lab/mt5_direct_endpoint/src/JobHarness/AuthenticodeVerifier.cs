using System.Runtime.InteropServices;
using System.Runtime.Versioning;
using System.Security.Cryptography;
using System.Security.Cryptography.X509Certificates;

namespace TradeJournal.Lab.JobHarness;

internal sealed record AuthenticodeVerification(
    bool Attempted,
    bool SignaturePresent,
    bool TrustValid,
    int? TrustProviderResult,
    string? SignerSubject,
    string? SignerThumbprintSha1,
    string? SignerThumbprintSha256,
    bool AllowlistMatched,
    string VerificationMode)
{
    public bool PreliminaryChecksPassed =>
        Attempted && SignaturePresent && TrustValid && AllowlistMatched;
}

internal sealed class AuthenticodeAllowlist
{
    private readonly IReadOnlyList<string> _subjects;
    private readonly IReadOnlyList<string> _thumbprints;

    public AuthenticodeAllowlist(
        IReadOnlyList<string> subjects,
        IReadOnlyList<string> thumbprints)
    {
        ArgumentNullException.ThrowIfNull(subjects);
        ArgumentNullException.ThrowIfNull(thumbprints);
        _subjects = subjects;
        _thumbprints = thumbprints;
    }

    public bool HasEntries => _subjects.Count > 0 || _thumbprints.Count > 0;

    public bool Matches(string? subject, string? thumbprintSha1, string? thumbprintSha256)
    {
        if (subject is not null
            && _subjects.Any(allowed => string.Equals(allowed, subject, StringComparison.OrdinalIgnoreCase)))
        {
            return true;
        }

        foreach (string allowed in _thumbprints)
        {
            string? observed = allowed.Length == 40 ? thumbprintSha1 : thumbprintSha256;
            if (observed is not null && FixedTimeHexEquals(allowed, observed))
            {
                return true;
            }
        }

        return false;
    }

    public static bool TryNormalizeThumbprint(string value, out string normalized)
    {
        ArgumentNullException.ThrowIfNull(value);
        Span<char> buffer = stackalloc char[value.Length];
        int written = 0;
        foreach (char character in value)
        {
            if (char.IsWhiteSpace(character) || character is ':' or '-')
            {
                continue;
            }

            if (!Uri.IsHexDigit(character))
            {
                normalized = string.Empty;
                return false;
            }

            buffer[written++] = char.ToUpperInvariant(character);
        }

        if (written is not 40 and not 64)
        {
            normalized = string.Empty;
            return false;
        }

        normalized = new string(buffer[..written]);
        return true;
    }

    private static bool FixedTimeHexEquals(string expected, string observed)
    {
        if (!TryNormalizeThumbprint(observed, out string normalizedObserved)
            || expected.Length != normalizedObserved.Length)
        {
            return false;
        }

        byte[] expectedBytes = Convert.FromHexString(expected);
        byte[] observedBytes = Convert.FromHexString(normalizedObserved);
        return CryptographicOperations.FixedTimeEquals(expectedBytes, observedBytes);
    }
}

internal static class AuthenticodeVerifier
{
    private const string VerificationMode = "WINVERIFYTRUST_CACHE_ONLY_WHOLE_CHAIN_REVOCATION";
    private const int ErrorSuccess = 0;
    private static readonly Guid GenericVerifyV2 = new("00AAC56B-CD44-11d0-8CC2-00C04FC295EE");

    public static AuthenticodeVerification Verify(
        string executablePath,
        AuthenticodeAllowlist allowlist)
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(executablePath);
        ArgumentNullException.ThrowIfNull(allowlist);

        if (!OperatingSystem.IsWindows())
        {
            return new AuthenticodeVerification(
                Attempted: false,
                SignaturePresent: false,
                TrustValid: false,
                TrustProviderResult: null,
                SignerSubject: null,
                SignerThumbprintSha1: null,
                SignerThumbprintSha256: null,
                AllowlistMatched: false,
                VerificationMode: "UNSUPPORTED_PLATFORM");
        }

        return VerifyOnWindows(executablePath, allowlist);
    }

    [SupportedOSPlatform("windows")]
    private static AuthenticodeVerification VerifyOnWindows(
        string executablePath,
        AuthenticodeAllowlist allowlist)
    {
        string? subject = null;
        string? subjectForMatching = null;
        string? thumbprintSha1 = null;
        string? thumbprintSha256 = null;
        bool signaturePresent = false;

        try
        {
            using X509Certificate signer = X509Certificate.CreateFromSignedFile(executablePath);
            using var signerCertificate = new X509Certificate2(signer);
            signaturePresent = true;
            subjectForMatching = signerCertificate.Subject;
            subject = SanitizeSubject(subjectForMatching);
            thumbprintSha1 = NormalizeObservedThumbprint(signerCertificate.Thumbprint);
            thumbprintSha256 = NormalizeObservedThumbprint(
                signerCertificate.GetCertHashString(HashAlgorithmName.SHA256));
        }
        catch (CryptographicException)
        {
            // WinVerifyTrust below remains authoritative. A missing extractable signer is
            // independently launch-blocking even if a provider unexpectedly returns success.
        }

        int trustResult = VerifyEmbeddedSignature(executablePath);
        bool trustValid = trustResult == ErrorSuccess && signaturePresent;
        bool allowlistMatched = signaturePresent
            && allowlist.Matches(subjectForMatching, thumbprintSha1, thumbprintSha256);

        return new AuthenticodeVerification(
            Attempted: true,
            SignaturePresent: signaturePresent,
            TrustValid: trustValid,
            TrustProviderResult: trustResult,
            SignerSubject: subject,
            SignerThumbprintSha1: thumbprintSha1,
            SignerThumbprintSha256: thumbprintSha256,
            AllowlistMatched: allowlistMatched,
            VerificationMode: VerificationMode);
    }

    [SupportedOSPlatform("windows")]
    private static int VerifyEmbeddedSignature(string executablePath)
    {
        IntPtr pathPointer = IntPtr.Zero;
        IntPtr fileInfoPointer = IntPtr.Zero;
        try
        {
            pathPointer = Marshal.StringToCoTaskMemUni(executablePath);
            var fileInfo = new WinTrustFileInfo
            {
                StructSize = checked((uint)Marshal.SizeOf<WinTrustFileInfo>()),
                FilePath = pathPointer,
                FileHandle = IntPtr.Zero,
                KnownSubject = IntPtr.Zero,
            };

            fileInfoPointer = Marshal.AllocHGlobal(Marshal.SizeOf<WinTrustFileInfo>());
            Marshal.StructureToPtr(fileInfo, fileInfoPointer, deleteOld: false);

            var trustData = new WinTrustData
            {
                StructSize = checked((uint)Marshal.SizeOf<WinTrustData>()),
                UiChoice = WinTrustUiChoice.None,
                RevocationChecks = WinTrustRevocationChecks.WholeChain,
                UnionChoice = WinTrustUnionChoice.File,
                FileInfo = fileInfoPointer,
                StateAction = WinTrustStateAction.Ignore,
                ProviderFlags = WinTrustProviderFlags.Safer
                    | WinTrustProviderFlags.RevocationCheckChain
                    | WinTrustProviderFlags.CacheOnlyUrlRetrieval,
                UiContext = 0,
            };

            Guid policy = GenericVerifyV2;
            return WinVerifyTrust(new IntPtr(-1), ref policy, ref trustData);
        }
        finally
        {
            if (fileInfoPointer != IntPtr.Zero)
            {
                Marshal.FreeHGlobal(fileInfoPointer);
            }

            if (pathPointer != IntPtr.Zero)
            {
                Marshal.FreeCoTaskMem(pathPointer);
            }
        }
    }

    private static string? NormalizeObservedThumbprint(string? value) =>
        value is not null && AuthenticodeAllowlist.TryNormalizeThumbprint(value, out string normalized)
            ? normalized
            : null;

    private static string SanitizeSubject(string subject)
    {
        const int maximumLength = 512;
        string sanitized = new(subject
            .Take(maximumLength)
            .Select(character => char.IsControl(character) ? '?' : character)
            .ToArray());
        return sanitized;
    }

    [DllImport("wintrust.dll", ExactSpelling = true, PreserveSig = true)]
    private static extern int WinVerifyTrust(
        IntPtr windowHandle,
        ref Guid actionId,
        ref WinTrustData trustData);

    [StructLayout(LayoutKind.Sequential)]
    private struct WinTrustFileInfo
    {
        public uint StructSize;
        public IntPtr FilePath;
        public IntPtr FileHandle;
        public IntPtr KnownSubject;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct WinTrustData
    {
        public uint StructSize;
        public IntPtr PolicyCallbackData;
        public IntPtr SipClientData;
        public WinTrustUiChoice UiChoice;
        public WinTrustRevocationChecks RevocationChecks;
        public WinTrustUnionChoice UnionChoice;
        public IntPtr FileInfo;
        public WinTrustStateAction StateAction;
        public IntPtr StateData;
        public IntPtr UrlReference;
        public WinTrustProviderFlags ProviderFlags;
        public uint UiContext;
        public IntPtr SignatureSettings;
    }

    private enum WinTrustUiChoice : uint
    {
        None = 2,
    }

    private enum WinTrustRevocationChecks : uint
    {
        WholeChain = 1,
    }

    private enum WinTrustUnionChoice : uint
    {
        File = 1,
    }

    private enum WinTrustStateAction : uint
    {
        Ignore = 0,
    }

    [Flags]
    private enum WinTrustProviderFlags : uint
    {
        Safer = 0x00000100,
        RevocationCheckChain = 0x00000040,
        CacheOnlyUrlRetrieval = 0x00001000,
    }
}

using System.Runtime.Versioning;
using System.Security.AccessControl;
using System.Security.Cryptography;
using System.Security.Principal;
using TradeJournal.Lab.JobHarness;

namespace TradeJournal.Lab.JobHarness.Coordinator;

// 256-bit session secret, generated once per C012 session and shared with the IPC peer out
// of band via a private, owner-only file -- never transmitted over the channel itself.
// Save() never overwrites an existing file, mirroring MetadataWriter's write-once contract.
// Dispose() zeroes the in-memory copy.
public sealed class C012SessionSecret : IDisposable
{
    public const int LengthBytes = 32;

    private readonly byte[] _secret;
    private bool _disposed;

    private C012SessionSecret(byte[] secret)
    {
        _secret = secret;
    }

    public static C012SessionSecret Generate() => new(RandomNumberGenerator.GetBytes(LengthBytes));

    public static C012SessionSecret Load(string path)
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(path);
        byte[] bytes = File.ReadAllBytes(Path.GetFullPath(path));
        if (bytes.Length != LengthBytes)
        {
            throw new InvalidDataException("The session secret file does not contain a 256-bit value.");
        }

        return new C012SessionSecret(bytes);
    }

    public ReadOnlySpan<byte> Value
    {
        get
        {
            ObjectDisposedException.ThrowIf(_disposed, this);
            return _secret;
        }
    }

    // Writes the secret to a temp file in the same directory, applies the Windows ACL to
    // that temp file, then renames atomically. Renaming within the same NTFS volume
    // preserves the security descriptor, so the file is never visible at its final path
    // with default (non-restricted) permissions.
    public void Save(string path)
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(path);
        ObjectDisposedException.ThrowIf(_disposed, this);

        string fullPath = Path.GetFullPath(path);
        string? parent = Path.GetDirectoryName(fullPath);
        if (parent is null || !Directory.Exists(parent))
        {
            throw new IOException("The session secret parent directory must already exist.");
        }

        MetadataPathPolicy.Validate(parent);

        if (File.Exists(fullPath))
        {
            throw new IOException("The session secret file already exists and will not be overwritten.");
        }

        string temporaryPath = Path.Combine(parent, $".{Path.GetFileName(fullPath)}.{Guid.NewGuid():N}.tmp");
        try
        {
            using (var stream = new FileStream(
                       temporaryPath,
                       FileMode.CreateNew,
                       FileAccess.Write,
                       FileShare.None,
                       bufferSize: 64,
                       FileOptions.WriteThrough))
            {
                stream.Write(_secret);
                stream.Flush(flushToDisk: true);
            }

            TryApplyWindowsAcl(temporaryPath);
            File.Move(temporaryPath, fullPath, overwrite: false);
        }
        finally
        {
            if (File.Exists(temporaryPath))
            {
                File.Delete(temporaryPath);
            }
        }
    }

    public void Dispose()
    {
        if (_disposed)
        {
            return;
        }

        CryptographicOperations.ZeroMemory(_secret);
        _disposed = true;
    }

    private static void TryApplyWindowsAcl(string path)
    {
        if (OperatingSystem.IsWindows())
        {
            ApplyWindowsAcl(path);
        }
    }

    [SupportedOSPlatform("windows")]
    private static void ApplyWindowsAcl(string path)
    {
        var fileInfo = new FileInfo(path);
        FileSecurity security = fileInfo.GetAccessControl();
        security.SetAccessRuleProtection(isProtected: true, preserveInheritance: false);

        foreach (FileSystemAccessRule rule in security.GetAccessRules(
                     includeExplicit: true, includeInherited: true, targetType: typeof(SecurityIdentifier)))
        {
            security.RemoveAccessRule(rule);
        }

        SecurityIdentifier currentUser = WindowsIdentity.GetCurrent().User
            ?? throw new InvalidOperationException("The current Windows user SID is unavailable.");
        security.AddAccessRule(new FileSystemAccessRule(currentUser, FileSystemRights.FullControl, AccessControlType.Allow));
        fileInfo.SetAccessControl(security);
    }
}

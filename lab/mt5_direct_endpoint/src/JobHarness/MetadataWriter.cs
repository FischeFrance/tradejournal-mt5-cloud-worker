using System.Text.Json;

namespace TradeJournal.Lab.JobHarness;

internal sealed class MetadataWriter
{
    private static readonly JsonSerializerOptions SerializerOptions = new()
    {
        WriteIndented = true,
    };

    private readonly string _path;
    private bool _initialized;

    public MetadataWriter(string path)
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(path);
        _path = Path.GetFullPath(path);

        string? parent = Path.GetDirectoryName(_path);
        if (parent is null || !Directory.Exists(parent))
        {
            throw new IOException("The metadata parent directory must already exist.");
        }

        MetadataPathPolicy.Validate(parent);

        if (File.Exists(_path))
        {
            throw new IOException("The metadata file already exists and will not be overwritten.");
        }
    }

    public static string Serialize(HarnessMetadata metadata) =>
        JsonSerializer.Serialize(metadata, SerializerOptions);

    public void Write(HarnessMetadata metadata)
    {
        ArgumentNullException.ThrowIfNull(metadata);

        string temporaryPath = Path.Combine(
            Path.GetDirectoryName(_path)!,
            $".{Path.GetFileName(_path)}.{Guid.NewGuid():N}.tmp");

        try
        {
            byte[] bytes = JsonSerializer.SerializeToUtf8Bytes(metadata, SerializerOptions);
            using (var stream = new FileStream(
                       temporaryPath,
                       FileMode.CreateNew,
                       FileAccess.Write,
                       FileShare.None,
                       bufferSize: 16 * 1024,
                       FileOptions.WriteThrough))
            {
                stream.Write(bytes);
                stream.Flush(flushToDisk: true);
            }

            File.Move(temporaryPath, _path, overwrite: _initialized);
            _initialized = true;
        }
        finally
        {
            if (File.Exists(temporaryPath))
            {
                File.Delete(temporaryPath);
            }
        }
    }

    public bool TryWrite(HarnessMetadata metadata)
    {
        try
        {
            Write(metadata);
            return true;
        }
        catch (Exception exception) when (exception is IOException or UnauthorizedAccessException)
        {
            return false;
        }
    }
}

internal static class MetadataPathPolicy
{
    public static void Validate(string directoryPath)
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(directoryPath);
        if (!OperatingSystem.IsWindows())
        {
            return;
        }

        string fullPath = Path.GetFullPath(directoryPath);
        if (IsUncOrDevicePath(fullPath))
        {
            throw new IOException("The metadata directory must be on a local Windows volume.");
        }

        string? root = Path.GetPathRoot(fullPath);
        if (string.IsNullOrWhiteSpace(root))
        {
            throw new IOException("The metadata directory has no local volume root.");
        }

        var drive = new DriveInfo(root);
        if (drive.DriveType is not DriveType.Fixed and not DriveType.Removable and not DriveType.Ram)
        {
            throw new IOException("The metadata directory must be on a local writable volume.");
        }

        for (DirectoryInfo? current = new(fullPath); current is not null; current = current.Parent)
        {
            if (current.Attributes.HasFlag(FileAttributes.ReparsePoint))
            {
                throw new IOException("The metadata directory ancestry cannot contain reparse points.");
            }
        }
    }

    internal static bool IsUncOrDevicePath(string path) =>
        path.StartsWith(@"\\", StringComparison.Ordinal);
}

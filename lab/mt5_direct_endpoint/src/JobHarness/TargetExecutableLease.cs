using System.Security.Cryptography;

namespace TradeJournal.Lab.JobHarness;

internal sealed class TargetExecutableLease : IDisposable
{
    private readonly FileStream _stream;
    private readonly byte[] _sha256Bytes;

    private TargetExecutableLease(
        FileStream stream,
        string fileName,
        byte[] sha256Bytes,
        long sizeBytes,
        DateTimeOffset lastWriteUtc)
    {
        _stream = stream;
        _sha256Bytes = sha256Bytes;
        FileName = fileName;
        Sha256 = Convert.ToHexString(sha256Bytes).ToLowerInvariant();
        SizeBytes = sizeBytes;
        LastWriteUtc = lastWriteUtc;
    }

    public string FileName { get; }

    public string Sha256 { get; }

    public long SizeBytes { get; }

    public DateTimeOffset LastWriteUtc { get; }

    public static TargetExecutableLease Open(string executablePath)
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(executablePath);

        // FileShare.Read deliberately excludes write/delete sharing for this opened
        // file. It does not by itself prove canonical path/file identity or bind a
        // future created image to this handle; actual launch stays globally disabled.
        var stream = new FileStream(
            executablePath,
            FileMode.Open,
            FileAccess.Read,
            FileShare.Read,
            bufferSize: 64 * 1024,
            FileOptions.SequentialScan);

        try
        {
            byte[] digest = SHA256.HashData(stream);
            var file = new FileInfo(executablePath);
            return new TargetExecutableLease(
                stream,
                file.Name,
                digest,
                stream.Length,
                file.LastWriteTimeUtc);
        }
        catch
        {
            stream.Dispose();
            throw;
        }
    }

    public bool MatchesExpectedSha256(string expectedSha256)
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(expectedSha256);
        byte[] expectedBytes;
        try
        {
            expectedBytes = Convert.FromHexString(expectedSha256);
        }
        catch (FormatException)
        {
            return false;
        }

        return expectedBytes.Length == _sha256Bytes.Length
            && CryptographicOperations.FixedTimeEquals(expectedBytes, _sha256Bytes);
    }

    public void Dispose() => _stream.Dispose();
}

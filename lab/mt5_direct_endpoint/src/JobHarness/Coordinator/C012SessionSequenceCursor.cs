using System.Globalization;

namespace TradeJournal.Lab.JobHarness.Coordinator;

// SequenceNumber is "the next sequence number this session's client side should use" when
// Pending is false, or "the sequence number an unconfirmed attempt was made at" when Pending
// is true. A Pending cursor is a fail-closed marker: no c012-client invocation may act on the
// session until an operator resolves it (there is no automated recovery by design).
public sealed record C012SessionSequenceState(long SequenceNumber, bool Pending);

public sealed class C012SessionCursorBusyException : Exception
{
    public C012SessionCursorBusyException(string message, Exception innerException)
        : base(message, innerException)
    {
    }
}

// Owns the exclusive lock over one session's client-side sequence cursor for the lifetime of
// one c012-client mutating request. The lock lives in a separate, content-less file
// (session.sequence.lock) held open with FileShare.None for the whole call -- session.sequence
// itself is never held open across its own replace, because on Windows a still-open handle to
// a file blocks File.Move onto it. Every write to session.sequence uses the same
// temp-file-then-move discipline as C012SessionPaths.WriteSessionId, so a reader (including
// c012-client status, which never takes the lock) only ever observes a fully-written value,
// never a torn one.
public sealed class C012SessionSequenceCursor : IDisposable
{
    private readonly string _sequencePath;
    private FileStream? _lockStream;
    private bool _disposed;

    private C012SessionSequenceCursor(string sequencePath, FileStream lockStream)
    {
        _sequencePath = sequencePath;
        _lockStream = lockStream;
    }

    // Creates session.sequence (clean, sequence 1) and session.sequence.lock (empty) as part
    // of c012-host start's atomic startup. Both must not already exist, mirroring
    // C012SessionPaths.WriteSessionId's refuse-to-overwrite discipline; the caller is
    // responsible for deleting both on any later startup failure (atomic, all four session
    // files or none).
    public static void InitializeAtSessionStart(string sessionDir)
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(sessionDir);
        WriteAtomic(
            C012SessionPaths.SessionSequencePath(sessionDir),
            Format(new C012SessionSequenceState(1, Pending: false)),
            overwrite: false);
        CreateEmptyLockFile(C012SessionPaths.SessionSequenceLockPath(sessionDir));
    }

    // No blocking wait is attempted: a sharing violation is reported immediately as
    // C012SessionCursorBusyException, distinguishing "another c012-client operation for this
    // session is genuinely in flight" from every other failure mode.
    public static C012SessionSequenceCursor AcquireExclusive(string sessionDir)
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(sessionDir);
        string lockPath = C012SessionPaths.SessionSequenceLockPath(sessionDir);
        FileStream lockStream;
        try
        {
            lockStream = new FileStream(lockPath, FileMode.Open, FileAccess.ReadWrite, FileShare.None);
        }
        catch (IOException exception)
        {
            throw new C012SessionCursorBusyException(
                "Another c012-client operation is already in progress for this session.", exception);
        }

        return new C012SessionSequenceCursor(C012SessionPaths.SessionSequencePath(sessionDir), lockStream);
    }

    // Reads without taking the lock -- used only by c012-client status, which is documented
    // to never claim more than a best-effort local snapshot. The atomic-replace discipline on
    // every write means this can never observe a torn value, only a possibly-stale-but-
    // complete one. Returns null if the file is missing or its content is not a value this
    // type ever wrote (corruption is reported as "unknown", never guessed at).
    public static C012SessionSequenceState? TryReadSnapshot(string sessionDir)
    {
        string path = C012SessionPaths.SessionSequencePath(sessionDir);
        if (!File.Exists(path))
        {
            return null;
        }

        try
        {
            return Parse(File.ReadAllText(path, System.Text.Encoding.UTF8));
        }
        catch (Exception exception) when (exception is IOException or FormatException or UnauthorizedAccessException)
        {
            return null;
        }
    }

    public C012SessionSequenceState Read()
    {
        ObjectDisposedException.ThrowIf(_disposed, this);
        return Parse(File.ReadAllText(_sequencePath, System.Text.Encoding.UTF8));
    }

    // Write-ahead marker: must be flushed to disk before the caller sends anything over the
    // pipe. If the connection is lost, the response never arrives, or the client process
    // itself dies at any point after this call returns, session.sequence durably shows
    // PENDING and every future cursor holder refuses to proceed -- this is what makes "no
    // reuse after an ambiguous outcome" true regardless of when, in the risky network
    // exchange, the failure actually happens.
    public void WritePending(long sequenceNumber)
    {
        ObjectDisposedException.ThrowIf(_disposed, this);
        WriteAtomic(_sequencePath, Format(new C012SessionSequenceState(sequenceNumber, Pending: true)), overwrite: true);
    }

    // Called only after a fully-validated, authenticated response has been observed:
    // nextSequenceNumber is sequenceNumber+1 on Accepted, or sequenceNumber unchanged (clean,
    // safe to retry) on a definite Rejected.
    public void WriteClean(long nextSequenceNumber)
    {
        ObjectDisposedException.ThrowIf(_disposed, this);
        WriteAtomic(_sequencePath, Format(new C012SessionSequenceState(nextSequenceNumber, Pending: false)), overwrite: true);
    }

    public void Dispose()
    {
        _lockStream?.Dispose();
        _lockStream = null;
        _disposed = true;
    }

    private static void CreateEmptyLockFile(string lockPath)
    {
        using var stream = new FileStream(lockPath, FileMode.CreateNew, FileAccess.Write, FileShare.None);
    }

    // Same discipline as C012SessionPaths.WriteSessionId: a temp file in the same directory,
    // written, flushed to disk, then moved into place. session.sequence is never held open
    // across its own replace -- only session.sequence.lock is held open, by the caller, for
    // the whole operation.
    private static void WriteAtomic(string finalPath, string content, bool overwrite)
    {
        if (!overwrite && File.Exists(finalPath))
        {
            throw new IOException($"{Path.GetFileName(finalPath)} already exists and will not be overwritten.");
        }

        string directory = Path.GetDirectoryName(finalPath)
            ?? throw new InvalidOperationException("The sequence cursor path has no parent directory.");
        string temporaryPath = Path.Combine(directory, $".{Path.GetFileName(finalPath)}.{Guid.NewGuid():N}.tmp");
        try
        {
            byte[] bytes = System.Text.Encoding.UTF8.GetBytes(content);
            using (var stream = new FileStream(
                       temporaryPath,
                       FileMode.CreateNew,
                       FileAccess.Write,
                       FileShare.None,
                       bufferSize: 64,
                       FileOptions.WriteThrough))
            {
                stream.Write(bytes);
                stream.Flush(flushToDisk: true);
            }

            File.Move(temporaryPath, finalPath, overwrite);
        }
        finally
        {
            if (File.Exists(temporaryPath))
            {
                File.Delete(temporaryPath);
            }
        }
    }

    private static string Format(C012SessionSequenceState state) =>
        state.Pending
            ? $"PENDING:{state.SequenceNumber.ToString(CultureInfo.InvariantCulture)}"
            : state.SequenceNumber.ToString(CultureInfo.InvariantCulture);

    private static C012SessionSequenceState Parse(string text)
    {
        string trimmed = text.Trim();
        if (trimmed.StartsWith("PENDING:", StringComparison.Ordinal))
        {
            string remainder = trimmed["PENDING:".Length..];
            if (!long.TryParse(remainder, NumberStyles.None, CultureInfo.InvariantCulture, out long pendingValue))
            {
                throw new FormatException($"Malformed pending sequence cursor value: '{text}'.");
            }

            return new C012SessionSequenceState(pendingValue, Pending: true);
        }

        if (!long.TryParse(trimmed, NumberStyles.None, CultureInfo.InvariantCulture, out long value))
        {
            throw new FormatException($"Malformed sequence cursor value: '{text}'.");
        }

        return new C012SessionSequenceState(value, Pending: false);
    }
}

namespace TradeJournal.Lab.JobHarness.Coordinator;

// Purely local, file-based facts about a --session-dir: does it exist, are the two session
// files present, and (if session.id parses) what session id it names. Never says anything
// about whether a host process is actually alive or listening -- that is not observable
// from the filesystem alone, and c012-client status must not claim it is.
public sealed record C012SessionDirStatus(
    bool DirectoryExists,
    bool SessionIdFilePresent,
    bool SessionSecretFilePresent,
    Guid? SessionId);

public static class C012SessionPaths
{
    public const string SessionIdFileName = "session.id";
    public const string SessionSecretFileName = "session.secret";

    public static string SessionIdPath(string sessionDir) => Path.Combine(sessionDir, SessionIdFileName);

    public static string SessionSecretPath(string sessionDir) => Path.Combine(sessionDir, SessionSecretFileName);

    // Deterministic and derived only: both host and client compute the same pipe name from
    // the same session id independently, so no separate pipe-name file is ever written.
    public static string DerivePipeName(Guid sessionId) => $"TJLabC012-{sessionId:N}";

    // Same atomic write-once discipline as MetadataWriter/C012SessionSecret.Save: a temp
    // file in the same directory, flushed to disk, then renamed; never overwrites an
    // existing session.id.
    public static void WriteSessionId(string sessionDir, Guid sessionId)
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(sessionDir);

        string fullPath = SessionIdPath(sessionDir);
        if (File.Exists(fullPath))
        {
            throw new IOException("session.id already exists and will not be overwritten.");
        }

        string temporaryPath = Path.Combine(sessionDir, $".session.id.{Guid.NewGuid():N}.tmp");
        try
        {
            byte[] bytes = System.Text.Encoding.UTF8.GetBytes(sessionId.ToString("D"));
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

    public static Guid ReadSessionId(string sessionDir)
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(sessionDir);
        string text = File.ReadAllText(SessionIdPath(sessionDir), System.Text.Encoding.UTF8).Trim();
        return Guid.ParseExact(text, "D");
    }

    public static C012SessionDirStatus Inspect(string sessionDir)
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(sessionDir);

        if (!Directory.Exists(sessionDir))
        {
            return new C012SessionDirStatus(false, false, false, null);
        }

        bool idPresent = File.Exists(SessionIdPath(sessionDir));
        bool secretPresent = File.Exists(SessionSecretPath(sessionDir));

        Guid? sessionId = null;
        if (idPresent)
        {
            try
            {
                sessionId = ReadSessionId(sessionDir);
            }
            catch (Exception exception) when (exception is IOException or FormatException or UnauthorizedAccessException)
            {
                sessionId = null;
            }
        }

        return new C012SessionDirStatus(true, idPresent, secretPresent, sessionId);
    }

    // Best-effort deletion used only for startup rollback and end-of-session cleanup; never
    // throws, since a missing file at that point is not itself a new problem.
    public static void TryDelete(string path)
    {
        try
        {
            if (File.Exists(path))
            {
                File.Delete(path);
            }
        }
        catch (Exception exception) when (exception is IOException or UnauthorizedAccessException)
        {
        }
    }
}

using System.IO.Pipes;

namespace TradeJournal.Lab.JobHarness.Coordinator;

// c012-client c0-start|c1-query|c2-submit|status. Each of the three mutating verbs is one
// short-lived process: connect, send exactly one authenticated request, print the result,
// exit. Because no in-memory state survives between these separate processes, the sequence
// number each one must use is read from a persisted, exclusively-locked cursor
// (C012SessionSequenceCursor) rather than assumed -- see that type for the full write-ahead/
// fail-closed discipline. status never connects to the pipe and never claims the host is
// alive -- it reports only what the local session directory itself shows.
public static class C012ClientCli
{
    public const int ExitAccepted = 0;
    public const int ExitRejected = 1;
    public const int ExitTransportFailure = 2;

    private const int ConnectTimeoutSeconds = 10;

    public static int Run(string verb, string[] args, TextWriter output, TextWriter error) =>
        RunAsync(verb, args, output, error).GetAwaiter().GetResult();

    private static async Task<int> RunAsync(string verb, string[] args, TextWriter output, TextWriter error)
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(verb);
        ArgumentNullException.ThrowIfNull(args);
        ArgumentNullException.ThrowIfNull(output);
        ArgumentNullException.ThrowIfNull(error);

        string? sessionDir = ParseSessionDir(args);
        if (sessionDir is null)
        {
            error.WriteLine("Usage: JobHarness c012-client <c0-start|c1-query|c2-submit|status> --session-dir <path>");
            return ExitTransportFailure;
        }

        if (verb == "status")
        {
            return RunStatus(sessionDir, output);
        }

        (C012Control control, C012RequestType requestType)? mapped = MapVerb(verb);
        if (mapped is null)
        {
            error.WriteLine("Unknown verb. Expected one of: c0-start, c1-query, c2-submit, status.");
            return ExitTransportFailure;
        }

        C012SessionDirStatus status = C012SessionPaths.Inspect(sessionDir);
        if (!status.DirectoryExists || !status.SessionIdFilePresent || !status.SessionSecretFilePresent
            || !status.SessionSequenceFilePresent || !status.SessionSequenceLockFilePresent)
        {
            error.WriteLine("--session-dir does not contain a started session.");
            return ExitTransportFailure;
        }

        if (status.SessionId is not { } sessionId)
        {
            error.WriteLine("--session-dir does not contain a started session.");
            return ExitTransportFailure;
        }

        // Platform-independent validation (args, verb, directory state) runs first so it
        // gives a useful answer on any OS; only the actual cursor/pipe work below is
        // Windows-only.
        if (!OperatingSystem.IsWindows())
        {
            error.WriteLine("c012-client requires Windows for c0-start/c1-query/c2-submit.");
            return ExitTransportFailure;
        }

        C012SessionSequenceCursor cursor;
        try
        {
            cursor = C012SessionSequenceCursor.AcquireExclusive(sessionDir);
        }
        catch (C012SessionCursorBusyException exception)
        {
            error.WriteLine(exception.Message);
            return ExitTransportFailure;
        }

        try
        {
            return await SendOverAcquiredCursorAsync(
                    sessionDir, sessionId, mapped.Value.control, mapped.Value.requestType, verb, cursor, output, error)
                .ConfigureAwait(false);
        }
        finally
        {
            cursor.Dispose();
        }
    }

    // Everything here runs while the caller holds the session's exclusive sequence lock, so a
    // second concurrent c012-client invocation can never interleave with this one.
    private static async Task<int> SendOverAcquiredCursorAsync(
        string sessionDir,
        Guid sessionId,
        C012Control control,
        C012RequestType requestType,
        string verb,
        C012SessionSequenceCursor cursor,
        TextWriter output,
        TextWriter error)
    {
        C012SessionSequenceState cursorState;
        try
        {
            cursorState = cursor.Read();
        }
        catch (Exception exception) when (exception is IOException or FormatException or UnauthorizedAccessException)
        {
            error.WriteLine("Unable to read the session sequence cursor.");
            return ExitTransportFailure;
        }

        if (cursorState.Pending)
        {
            error.WriteLine(
                $"The session sequence cursor is ambiguous: an attempt at sequence {cursorState.SequenceNumber} " +
                "was made but never confirmed. This session cannot be safely continued via c012-client.");
            return ExitTransportFailure;
        }

        long expectedSequence = ExpectedSequenceForControl(control);
        if (cursorState.SequenceNumber != expectedSequence)
        {
            error.WriteLine(
                $"The session sequence cursor shows {cursorState.SequenceNumber}, but {verb} expects sequence " +
                $"{expectedSequence}. Refusing without contacting the host.");
            return ExitTransportFailure;
        }

        long sequenceNumber = cursorState.SequenceNumber;

        C012SessionSecret secret;
        try
        {
            secret = C012SessionSecret.Load(C012SessionPaths.SessionSecretPath(sessionDir));
        }
        catch (Exception exception) when (exception is IOException or UnauthorizedAccessException or InvalidDataException)
        {
            error.WriteLine("Unable to load the session secret.");
            return ExitTransportFailure;
        }

        try
        {
            string pipeName = C012SessionPaths.DerivePipeName(sessionId);
            using var pipe = new NamedPipeClientStream(".", pipeName, PipeDirection.InOut, PipeOptions.Asynchronous);
            using var connectTimeout = new CancellationTokenSource(TimeSpan.FromSeconds(ConnectTimeoutSeconds));
            try
            {
                await pipe.ConnectAsync(connectTimeout.Token).ConfigureAwait(false);
            }
            catch (OperationCanceledException)
            {
                // No request byte was ever sent: the cursor is untouched and this sequence
                // number remains perfectly safe to retry (e.g. while the host is still
                // starting up).
                error.WriteLine("Timed out connecting to the host.");
                return ExitTransportFailure;
            }

            // Write-ahead: durable on disk before any byte is sent. From this point on, a
            // lost response or a client crash leaves the cursor unambiguously PENDING rather
            // than silently reusable.
            cursor.WritePending(sequenceNumber);

            var client = new C012ClientChannel(pipe, secret, sessionId, sequenceNumber);
            C012TransitionResult result;
            try
            {
                result = await client.SendAsync(control, requestType, CancellationToken.None).ConfigureAwait(false);
            }
            catch (C012FramingException exception)
            {
                error.WriteLine($"Request failed: {exception.Message}. The session sequence cursor is now ambiguous.");
                return ExitTransportFailure;
            }

            cursor.WriteClean(result.Accepted ? sequenceNumber + 1 : sequenceNumber);

            output.WriteLine($"accepted={result.Accepted} resulting_state={result.ResultingState} reason={result.Reason}");
            return result.Accepted ? ExitAccepted : ExitRejected;
        }
        catch (IOException exception)
        {
            error.WriteLine($"Connection failed: {exception.Message}. The session sequence cursor may now be ambiguous.");
            return ExitTransportFailure;
        }
        finally
        {
            secret.Dispose();
        }
    }

    private static long ExpectedSequenceForControl(C012Control control) => control switch
    {
        C012Control.C0 => 1,
        C012Control.C1 => 2,
        C012Control.C2 => 3,
        _ => throw new ArgumentOutOfRangeException(nameof(control)),
    };

    private static int RunStatus(string sessionDir, TextWriter output)
    {
        C012SessionDirStatus status = C012SessionPaths.Inspect(sessionDir);
        output.WriteLine($"session_dir_valid={status.DirectoryExists}");
        output.WriteLine($"session_id_file_present={status.SessionIdFilePresent}");
        output.WriteLine($"session_secret_file_present={status.SessionSecretFilePresent}");
        output.WriteLine($"session_sequence_file_present={status.SessionSequenceFilePresent}");
        output.WriteLine($"session_sequence_lock_file_present={status.SessionSequenceLockFilePresent}");

        C012SessionSequenceState? sequenceState = status.SessionSequenceFilePresent
            ? C012SessionSequenceCursor.TryReadSnapshot(sessionDir)
            : null;
        output.WriteLine(sequenceState is { } sequence
            ? $"session_sequence_next={sequence.SequenceNumber} session_sequence_pending={sequence.Pending}"
            : "session_sequence_next=unknown session_sequence_pending=unknown");

        output.WriteLine(status.SessionId is { } id ? $"session_id={id:D}" : "session_id=unknown");
        return status.DirectoryExists && status.SessionIdFilePresent && status.SessionSecretFilePresent
            && status.SessionSequenceFilePresent && status.SessionSequenceLockFilePresent
            ? ExitAccepted
            : ExitRejected;
    }

    private static (C012Control control, C012RequestType requestType)? MapVerb(string verb) => verb switch
    {
        "c0-start" => (C012Control.C0, C012RequestType.Start),
        "c1-query" => (C012Control.C1, C012RequestType.Query),
        "c2-submit" => (C012Control.C2, C012RequestType.SubmitConfig),
        _ => null,
    };

    private static string? ParseSessionDir(string[] args)
    {
        for (int index = 0; index < args.Length - 1; index++)
        {
            if (args[index] == "--session-dir")
            {
                return args[index + 1];
            }
        }

        return null;
    }
}

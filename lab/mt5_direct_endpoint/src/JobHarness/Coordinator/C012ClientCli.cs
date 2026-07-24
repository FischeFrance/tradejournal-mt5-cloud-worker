using System.IO.Pipes;

namespace TradeJournal.Lab.JobHarness.Coordinator;

// c012-client c0-start|c1-query|c2-submit|status. Each of the three mutating verbs is one
// short-lived process: connect, send exactly one authenticated request, print the result,
// exit. status never connects to the pipe and never claims the host is alive -- it reports
// only what the local session directory itself shows.
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
        if (!status.DirectoryExists || !status.SessionIdFilePresent || !status.SessionSecretFilePresent)
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
        // gives a useful answer on any OS; only the actual pipe connection below is
        // Windows-only.
        if (!OperatingSystem.IsWindows())
        {
            error.WriteLine("c012-client requires Windows for c0-start/c1-query/c2-submit.");
            return ExitTransportFailure;
        }

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
                error.WriteLine("Timed out connecting to the host.");
                return ExitTransportFailure;
            }

            var client = new C012ClientChannel(pipe, secret, sessionId);
            C012TransitionResult result;
            try
            {
                result = await client.SendAsync(mapped.Value.control, mapped.Value.requestType, CancellationToken.None)
                    .ConfigureAwait(false);
            }
            catch (C012FramingException exception)
            {
                error.WriteLine($"Request failed: {exception.Message}");
                return ExitTransportFailure;
            }

            output.WriteLine($"accepted={result.Accepted} resulting_state={result.ResultingState} reason={result.Reason}");
            return result.Accepted ? ExitAccepted : ExitRejected;
        }
        catch (IOException exception)
        {
            error.WriteLine($"Connection failed: {exception.Message}");
            return ExitTransportFailure;
        }
        finally
        {
            secret.Dispose();
        }
    }

    private static int RunStatus(string sessionDir, TextWriter output)
    {
        C012SessionDirStatus status = C012SessionPaths.Inspect(sessionDir);
        output.WriteLine($"session_dir_valid={status.DirectoryExists}");
        output.WriteLine($"session_id_file_present={status.SessionIdFilePresent}");
        output.WriteLine($"session_secret_file_present={status.SessionSecretFilePresent}");
        output.WriteLine(status.SessionId is { } id ? $"session_id={id:D}" : "session_id=unknown");
        return status.DirectoryExists && status.SessionIdFilePresent && status.SessionSecretFilePresent
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

using System.IO.Pipes;
using System.Runtime.Versioning;
using System.Security.AccessControl;
using System.Security.Principal;

namespace TradeJournal.Lab.JobHarness.Coordinator;

// c012-host start: owns the Named Pipe and the C012RequestSequencer for one C012 session.
// Session startup is transactional -- session.id is written first, but is rolled back
// (deleted, along with any secret file already written) if any later step fails, so a
// --session-dir is only ever left holding a fully-initialized, listening session or no
// session at all. Uses C012NotImplementedRootProcessLauncher exclusively: no Job Object, no
// process, no MT5/MetaEditor is ever started by this class.
public static class C012HostCli
{
    public const int ExitTerminated = 0;
    public const int ExitFailedClosed = 1;
    public const int ExitStartupFailure = 2;

    private const int DefaultIdleTimeoutSeconds = 300;
    private const int ConsecutiveUnproductiveFailureCap = 3;
    private const int PipeBufferSize = 4096;

    public static int Run(string[] args, TextWriter output, TextWriter error) =>
        RunAsync(args, output, error).GetAwaiter().GetResult();

    private static async Task<int> RunAsync(string[] args, TextWriter output, TextWriter error)
    {
        ArgumentNullException.ThrowIfNull(args);
        ArgumentNullException.ThrowIfNull(output);
        ArgumentNullException.ThrowIfNull(error);

        string? sessionDir = ParseSessionDir(args);
        if (sessionDir is null)
        {
            error.WriteLine("Usage: JobHarness c012-host start --session-dir <path>");
            return ExitStartupFailure;
        }

        C012SessionDirStatus status = C012SessionPaths.Inspect(sessionDir);
        if (!status.DirectoryExists)
        {
            error.WriteLine("--session-dir does not exist.");
            return ExitStartupFailure;
        }

        if (status.SessionIdFilePresent || status.SessionSecretFilePresent)
        {
            error.WriteLine("--session-dir already contains a session; refusing to reuse or overwrite it.");
            return ExitStartupFailure;
        }

        // Platform-independent validation (args, directory state) runs first so it gives a
        // useful answer on any OS; only the actual secret/pipe work below is Windows-only.
        if (!OperatingSystem.IsWindows())
        {
            error.WriteLine("c012-host requires Windows.");
            return ExitStartupFailure;
        }

        Guid sessionId = Guid.NewGuid();
        try
        {
            C012SessionPaths.WriteSessionId(sessionDir, sessionId);
        }
        catch (Exception exception) when (exception is IOException or UnauthorizedAccessException)
        {
            error.WriteLine("Unable to write session.id.");
            return ExitStartupFailure;
        }

        C012SessionSecret? secret = null;
        NamedPipeServerStream? pipe = null;
        try
        {
            secret = C012SessionSecret.Generate();
            secret.Save(C012SessionPaths.SessionSecretPath(sessionDir));
            string pipeName = C012SessionPaths.DerivePipeName(sessionId);
            pipe = CreateAclRestrictedPipe(pipeName);
        }
        catch (Exception)
        {
            // Rollback: any failure between "session.id written" and "listening" must leave
            // the session directory exactly as empty as it started, so a retry of
            // c012-host start on the same directory is a genuine fresh start, not a partial
            // reuse.
            C012SessionPaths.TryDelete(C012SessionPaths.SessionIdPath(sessionDir));
            C012SessionPaths.TryDelete(C012SessionPaths.SessionSecretPath(sessionDir));
            secret?.Dispose();
            pipe?.Dispose();
            error.WriteLine("Unable to initialize the session (secret, ACL, or pipe creation failed); rolled back.");
            return ExitStartupFailure;
        }

        if (secret is null || pipe is null)
        {
            // Structurally unreachable: the catch above already returns on any failure to
            // assign both. Kept as an explicit, fail-closed guard rather than trusting that
            // invariant silently.
            secret?.Dispose();
            pipe?.Dispose();
            error.WriteLine("Unable to initialize the session.");
            return ExitStartupFailure;
        }

        // From here on, initialization is complete and both files are the confirmed,
        // durable session -- session.id is never rolled back again after this point.
        var sequencer = new C012RequestSequencer(sessionId);
        var launcher = new C012NotImplementedRootProcessLauncher();
        var processor = new C012OrchestratingProcessor(sequencer, launcher);

        int exitCode;
        try
        {
            exitCode = await ListenLoopAsync(pipe, secret, processor, sequencer).ConfigureAwait(false);
        }
        finally
        {
            C012SessionPaths.TryDelete(C012SessionPaths.SessionSecretPath(sessionDir));
            secret.Dispose();
            pipe.Dispose();
        }

        return exitCode;
    }

    private static async Task<int> ListenLoopAsync(
        NamedPipeServerStream pipe,
        C012SessionSecret secret,
        C012OrchestratingProcessor processor,
        C012RequestSequencer sequencer)
    {
        using var idleTimeout = new CancellationTokenSource(TimeSpan.FromSeconds(DefaultIdleTimeoutSeconds));
        int consecutiveUnproductiveFailures = 0;

        while (sequencer.CurrentState is not C012State.Terminated and not C012State.FailedClosed)
        {
            try
            {
                await pipe.WaitForConnectionAsync(idleTimeout.Token).ConfigureAwait(false);
            }
            catch (OperationCanceledException)
            {
                sequencer.ApplyInternal(C012Trigger.Timeout);
                break;
            }

            var channel = new C012ServerChannel(pipe, secret, processor);
            C012ChannelOutcome outcome;
            try
            {
                outcome = await channel.ProcessNextRequestAsync(idleTimeout.Token).ConfigureAwait(false);
            }
            catch (OperationCanceledException)
            {
                TryDisconnect(pipe);
                sequencer.ApplyInternal(C012Trigger.Timeout);
                break;
            }

            TryDisconnect(pipe);

            if (outcome is C012ChannelOutcome.Malformed or C012ChannelOutcome.AuthenticationFailed)
            {
                consecutiveUnproductiveFailures++;
                bool afterC2Dispatch = sequencer.CurrentState
                    is C012State.C2Submitting or C012State.C2LoginWindow or C012State.C2Teardown;
                if (afterC2Dispatch || consecutiveUnproductiveFailures > ConsecutiveUnproductiveFailureCap)
                {
                    sequencer.ApplyInternal(C012Trigger.OperationFailed);
                    break;
                }
            }
            else if (outcome == C012ChannelOutcome.RequestProcessed)
            {
                consecutiveUnproductiveFailures = 0;
            }

            // PeerDisconnected (clean EOF, no bytes sent, no mutation possible) never counts
            // toward the cap and never breaks the loop on its own.
        }

        return sequencer.CurrentState == C012State.Terminated ? ExitTerminated : ExitFailedClosed;
    }

    private static void TryDisconnect(NamedPipeServerStream pipe)
    {
        try
        {
            if (pipe.IsConnected)
            {
                pipe.Disconnect();
            }
        }
        catch (IOException)
        {
        }
    }

    [SupportedOSPlatform("windows")]
    private static NamedPipeServerStream CreateAclRestrictedPipe(string pipeName)
    {
        var security = new PipeSecurity();
        SecurityIdentifier currentUser = WindowsIdentity.GetCurrent().User
            ?? throw new InvalidOperationException("The current Windows user SID is unavailable.");
        security.SetAccessRuleProtection(isProtected: true, preserveInheritance: false);
        security.AddAccessRule(new PipeAccessRule(currentUser, PipeAccessRights.ReadWrite, AccessControlType.Allow));

        return new NamedPipeServerStream(
            pipeName,
            PipeDirection.InOut,
            maxNumberOfServerInstances: 1,
            PipeTransmissionMode.Byte,
            PipeOptions.Asynchronous,
            inBufferSize: PipeBufferSize,
            outBufferSize: PipeBufferSize,
            security);
    }

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

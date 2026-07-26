using System.IO.Pipes;
using System.Reflection;
using System.Runtime.Versioning;
using System.Security.AccessControl;
using System.Security.Cryptography;
using System.Security.Principal;

namespace TradeJournal.Lab.JobHarness.Coordinator;

// c012-host start / start-innocuous: owns the Named Pipe and the C012RequestSequencer for one
// C012 session. Session startup is transactional -- session.id is written first, but is rolled
// back (deleted, along with any secret file already written) if any later step fails, so a
// --session-dir is only ever left holding a fully-initialized, listening session or no session
// at all. `Run` (3-arg, what Program.cs's "c012-host start" arm calls) uses
// C012NotImplementedRootProcessLauncher exclusively: no Job Object, no process, no
// MT5/MetaEditor is ever started through it. The launcher-injecting `Run` overload (5-arg) lets
// a caller supply any IC012RootProcessLauncher through this same host loop; besides Windows-only
// tests, its only other caller anywhere is RunInnocuous below, which is itself reachable only
// from Program.cs's separate "c012-host start-innocuous" arm and only ever constructs
// C012InnocuousRootProcessLauncher pinned to this process re-invoking itself harmlessly (see
// RunInnocuous's own header comment) -- "c012-host start" itself is untouched by any of this.
public static class C012HostCli
{
    public const int ExitTerminated = 0;
    public const int ExitFailedClosed = 1;
    public const int ExitStartupFailure = 2;

    private const int DefaultIdleTimeoutSeconds = 300;
    private const int ConsecutiveUnproductiveFailureCap = 3;
    private const int PipeBufferSize = 4096;

    public static int Run(string[] args, TextWriter output, TextWriter error) =>
        RunAsync(args, output, error, new C012NotImplementedRootProcessLauncher(), TimeSpan.FromSeconds(DefaultIdleTimeoutSeconds))
            .GetAwaiter().GetResult();

    // Test-only entry point: identical to the 3-argument overload except that the caller
    // supplies the launcher instead of always getting C012NotImplementedRootProcessLauncher,
    // and may optionally shorten the idle timeout (still 300s by default) so Windows-only
    // tests can exercise real timeout behavior without waiting minutes. Not called from
    // Program.cs and not reachable from any CLI argument a user could type.
    public static int Run(
        string[] args,
        TextWriter output,
        TextWriter error,
        IC012RootProcessLauncher launcher,
        TimeSpan? idleTimeout = null) =>
        RunAsync(args, output, error, launcher, idleTimeout ?? TimeSpan.FromSeconds(DefaultIdleTimeoutSeconds))
            .GetAwaiter().GetResult();

    // Test-only/harmless smoke-testing verb (c012-host start-innocuous): a narrow, explicitly
    // authorized exception to "c012-host start never gains real-launch capability" (see the
    // class header comment and lab/mt5_direct_endpoint/AGENTS.md), added solely so a Windows
    // smoke test can drive a real Job-Object-contained process end to end. Unlike every other
    // entry point on this class, this one accepts no caller-supplied executable path or SHA-256
    // at all: the only allowed --target resolves to this very process re-invoking itself with a
    // fixed, harmless flag (see Program.cs's --innocent-lab-sleeper/--innocent-lab-exit-zero),
    // so both the executable and its expected hash are always computed here from the real
    // running binary, never trusted from a command line. C012InnocuousRootProcessLauncher's own
    // constructor-time refusal of terminal(64)/metaeditor(64) names still applies underneath
    // this as an independent second check. c012-host start's dispatch, behavior, and launcher
    // (C012NotImplementedRootProcessLauncher) are completely unaffected by this method.
    public const string InnocuousTargetSelfSleeper = "self-sleeper";

    private static readonly TimeSpan InnocuousSubmitterWaitTimeout = TimeSpan.FromSeconds(10);

    public static int RunInnocuous(string[] args, TextWriter output, TextWriter error)
    {
        ArgumentNullException.ThrowIfNull(args);
        ArgumentNullException.ThrowIfNull(output);
        ArgumentNullException.ThrowIfNull(error);

        string? sessionDir = ParseSessionDir(args);
        string? target = ParseTarget(args);
        if (sessionDir is null || target is null)
        {
            error.WriteLine("Usage: JobHarness c012-host start-innocuous --session-dir <path> --target <key>");
            error.WriteLine($"Allowed --target values: {InnocuousTargetSelfSleeper}");
            return ExitStartupFailure;
        }

        if (target != InnocuousTargetSelfSleeper)
        {
            error.WriteLine($"Unknown --target '{target}'. Allowed values: {InnocuousTargetSelfSleeper}.");
            return ExitStartupFailure;
        }

        if (!OperatingSystem.IsWindows())
        {
            error.WriteLine("c012-host requires Windows.");
            return ExitStartupFailure;
        }

        string executable = Environment.ProcessPath
            ?? throw new InvalidOperationException("Current process path is unavailable.");

        IC012RootProcessLauncher launcher;
        try
        {
            launcher = new C012InnocuousRootProcessLauncher(
                executable,
                ComputeSha256(executable),
                SelfInvocationArguments(executable, "--innocent-lab-sleeper"),
                SelfInvocationArguments(executable, "--innocent-lab-exit-zero"),
                InnocuousSubmitterWaitTimeout);
        }
        catch (InvalidOperationException exception)
        {
            // Structurally unreachable (this running process is never named
            // terminal(64)/metaeditor(64)), but kept as an explicit fail-closed guard rather
            // than trusting that invariant silently.
            error.WriteLine($"Refusing to start: {exception.Message}");
            return ExitStartupFailure;
        }

        return Run(["--session-dir", sessionDir], output, error, launcher);
    }

    private static string? ParseTarget(string[] args)
    {
        for (int index = 0; index < args.Length - 1; index++)
        {
            if (args[index] == "--target")
            {
                return args[index + 1];
            }
        }

        return null;
    }

    // Mirrors the self-invocation convention already proven by
    // JobHarness.Coordinator.Tests/Program.cs's SelfInvocationArguments: when the running
    // process host is the "dotnet" muxer rather than a published apphost, the target assembly
    // path must be prepended so the re-invoked process actually runs JobHarness again instead
    // of just "dotnet" with no program to load.
    private static List<string> SelfInvocationArguments(string executable, params string[] trailingArguments)
    {
        var arguments = new List<string>();
        if (Path.GetFileNameWithoutExtension(executable).Equals("dotnet", StringComparison.OrdinalIgnoreCase))
        {
            arguments.Add(Assembly.GetExecutingAssembly().Location);
        }

        arguments.AddRange(trailingArguments);
        return arguments;
    }

    private static string ComputeSha256(string path)
    {
        using FileStream stream = File.OpenRead(path);
        return Convert.ToHexString(SHA256.HashData(stream)).ToLowerInvariant();
    }

    private static async Task<int> RunAsync(
        string[] args, TextWriter output, TextWriter error, IC012RootProcessLauncher launcher, TimeSpan idleTimeout)
    {
        ArgumentNullException.ThrowIfNull(args);
        ArgumentNullException.ThrowIfNull(output);
        ArgumentNullException.ThrowIfNull(error);
        ArgumentNullException.ThrowIfNull(launcher);

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

        if (status.SessionIdFilePresent || status.SessionSecretFilePresent
            || status.SessionSequenceFilePresent || status.SessionSequenceLockFilePresent)
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
            C012SessionSequenceCursor.InitializeAtSessionStart(sessionDir);
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
            // reuse. All four session files are created and rolled back together.
            C012SessionPaths.TryDelete(C012SessionPaths.SessionIdPath(sessionDir));
            C012SessionPaths.TryDelete(C012SessionPaths.SessionSecretPath(sessionDir));
            C012SessionPaths.TryDelete(C012SessionPaths.SessionSequencePath(sessionDir));
            C012SessionPaths.TryDelete(C012SessionPaths.SessionSequenceLockPath(sessionDir));
            secret?.Dispose();
            pipe?.Dispose();
            error.WriteLine("Unable to initialize the session (secret, ACL, sequence cursor, or pipe creation failed); rolled back.");
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

        // From here on, initialization is complete and all four files are the confirmed,
        // durable session -- session.id is never rolled back again after this point.
        var sequencer = new C012RequestSequencer(sessionId);
        var processor = new C012OrchestratingProcessor(sequencer, launcher);

        int exitCode;
        try
        {
            exitCode = await ListenLoopAsync(pipe, secret, processor, sequencer, idleTimeout).ConfigureAwait(false);
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
        C012RequestSequencer sequencer,
        TimeSpan idleTimeout)
    {
        using var idleTimeoutSource = new CancellationTokenSource(idleTimeout);
        int consecutiveUnproductiveFailures = 0;

        while (sequencer.CurrentState is not C012State.Terminated and not C012State.FailedClosed)
        {
            try
            {
                await pipe.WaitForConnectionAsync(idleTimeoutSource.Token).ConfigureAwait(false);
            }
            catch (OperationCanceledException)
            {
                // Every host-originated fail-closed path goes through the orchestrator, never
                // straight through the sequencer: only FailFromHost tears down a real
                // Job/root/submitter, and it does so idempotently.
                processor.FailFromHost(C012Trigger.Timeout);
                break;
            }
            catch (Exception)
            {
                // An unexpected failure while merely waiting for a connection is still a
                // host-loop-level error, not a normal protocol outcome: fail closed and tear
                // down rather than let it escape uncontrolled or leave the Job/root/submitter
                // running with nothing left listening for them.
                processor.FailFromHost(C012Trigger.OperationFailed);
                break;
            }

            var channel = new C012ServerChannel(pipe, secret, processor);
            C012ChannelOutcome outcome;
            try
            {
                outcome = await channel.ProcessNextRequestAsync(idleTimeoutSource.Token).ConfigureAwait(false);
            }
            catch (OperationCanceledException)
            {
                TryDisconnect(pipe);
                processor.FailFromHost(C012Trigger.Timeout);
                break;
            }
            catch (Exception)
            {
                TryDisconnect(pipe);
                processor.FailFromHost(C012Trigger.OperationFailed);
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
                    processor.FailFromHost(C012Trigger.OperationFailed);
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

    // Always attempts the reset, never gated on IsConnected: after a response write fails
    // because the peer already severed the connection, IsConnected is not guaranteed to have
    // already flipped to false (it is a .NET-tracked flag, not necessarily updated by a
    // failed I/O operation), so gating on it here could skip the one call that frees this
    // pipe instance for the next WaitForConnectionAsync. Catches any exception, not only
    // IOException: disconnecting an already-broken or never-fully-connected pipe instance can
    // surface other exception types, and none of them may be allowed to escape uncaught --
    // doing so would silently stop the whole host loop from ever listening again.
    private static void TryDisconnect(NamedPipeServerStream pipe)
    {
        try
        {
            pipe.Disconnect();
        }
        catch (Exception)
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

        // Verified signature (System.IO.Pipes.AccessControl, NamedPipeServerStreamAcl.Create):
        // https://learn.microsoft.com/en-us/dotnet/api/system.io.pipes.namedpipeserverstreamacl.create
        // Create(string pipeName, PipeDirection direction, int maxNumberOfServerInstances,
        //   PipeTransmissionMode transmissionMode, PipeOptions options, int inBufferSize,
        //   int outBufferSize, PipeSecurity? pipeSecurity,
        //   HandleInheritability inheritability = HandleInheritability.None,
        //   PipeAccessRights additionalAccessRights = 0)
        // inheritability is passed explicitly even though it matches the default: the
        // pipe handle must never be inheritable by a future child process.
        return NamedPipeServerStreamAcl.Create(
            pipeName,
            PipeDirection.InOut,
            1,
            PipeTransmissionMode.Byte,
            PipeOptions.Asynchronous,
            PipeBufferSize,
            PipeBufferSize,
            security,
            HandleInheritability.None);
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

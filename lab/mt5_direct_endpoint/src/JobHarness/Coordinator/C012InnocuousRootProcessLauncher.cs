using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;
using System.Text;
using TradeJournal.Lab.JobHarness;

namespace TradeJournal.Lab.JobHarness.Coordinator;

// Real Windows launcher for IC012RootProcessLauncher, restricted structurally to a single,
// caller-pinned, non-MT5 executable. Reuses the exact CreateProcess/AssignProcessToJobObject
// /ResumeThread sequence and KILL_ON_JOB_CLOSE verification already proven in
// JobObjectRunner.cs, adapted to a persistent, multi-step launcher interface instead of one
// self-contained method. JobObjectRunner.cs itself is not modified or reused: its
// ConfigureAndVerifyJob/GetCreationTime logic is duplicated here in miniature by explicit
// B4.3 design decision (avoids widening JobObjectRunner's visibility for a single caller).
//
// Not wired into C012HostCli's production `c012-host start` path -- that path still uses only
// C012NotImplementedRootProcessLauncher, untouched. Reachable in production solely through
// `c012-host start-innocuous` (C012HostCli.RunInnocuous), a narrow, explicitly authorized
// exception that only ever constructs this class pinned to the running JobHarness process
// re-invoking itself with a fixed, harmless flag -- never a caller-supplied path or hash.
// Windows-only tests also drive it directly via C012HostCli.Run's launcher-injecting overload.
// This class has no credential, config-file, or network parameter anywhere in its surface.
public sealed class C012InnocuousRootProcessLauncher : IC012RootProcessLauncher
{
    private const uint FailureExitCode = 125;
    private const uint TeardownExitCode = 126;

    // Unconditional, construction-time refusal: independent of whatever path/hash a caller
    // supplies, this launcher never even comes into existence configured against something
    // named like an MT5 executable.
    private static readonly string[] BlockedFileNames =
    [
        "terminal.exe",
        "terminal64.exe",
        "metaeditor.exe",
        "metaeditor64.exe",
    ];

    private readonly string _executablePath;
    private readonly string _expectedSha256Hex;
    private readonly IReadOnlyList<string> _rootArguments;
    private readonly IReadOnlyList<string> _submitterArguments;
    private readonly TimeSpan _submitterWaitTimeout;

    private NativeProcessState? _rootState;

    public C012InnocuousRootProcessLauncher(
        string executablePath,
        string expectedSha256Hex,
        IReadOnlyList<string> rootArguments,
        IReadOnlyList<string> submitterArguments,
        TimeSpan submitterWaitTimeout)
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(executablePath);
        ArgumentException.ThrowIfNullOrWhiteSpace(expectedSha256Hex);
        ArgumentNullException.ThrowIfNull(rootArguments);
        ArgumentNullException.ThrowIfNull(submitterArguments);
        ArgumentOutOfRangeException.ThrowIfLessThanOrEqual(submitterWaitTimeout, TimeSpan.Zero);

        string fileName = Path.GetFileName(executablePath);
        if (BlockedFileNames.Any(blocked => fileName.Equals(blocked, StringComparison.OrdinalIgnoreCase)))
        {
            throw new InvalidOperationException(
                "This launcher refuses to be configured against an executable named like " +
                "terminal.exe/terminal64.exe/metaeditor.exe/metaeditor64.exe.");
        }

        _executablePath = executablePath;
        _expectedSha256Hex = expectedSha256Hex;
        _rootArguments = rootArguments;
        _submitterArguments = submitterArguments;
        _submitterWaitTimeout = submitterWaitTimeout;
    }

    // Known only once LaunchSuspendedRoot has returned; exposed purely so a Windows-only
    // smoke test can independently observe the real root process (e.g. confirm it is gone
    // after teardown) without this class ever exposing its internal native handle types.
    public uint? RootProcessId { get; private set; }

    public C012JobToken CreateJob()
    {
        SafeJobHandle job = NativeMethods.CreateJobObject(IntPtr.Zero, name: null);
        if (job.IsInvalid)
        {
            throw LastWin32("CreateJobObjectW");
        }

        try
        {
            ConfigureAndVerifyKillOnJobClose(job);
        }
        catch
        {
            job.Dispose();
            throw;
        }

        return new C012JobToken(job);
    }

    public C012ProcessToken LaunchSuspendedRoot(C012JobToken job)
    {
        C012ProcessToken token = LaunchSuspended(_rootArguments);
        NativeProcessState state = Unwrap<NativeProcessState>(token.NativeHandle);
        _rootState = state;
        RootProcessId = state.ProcessId;
        return token;
    }

    public void AssignRootToJob(C012JobToken job, C012ProcessToken root) => Assign(job, root);

    public void ResumeRoot(C012ProcessToken root) => Resume(root);

    public bool VerifyRootAlive(C012JobToken job, C012ProcessToken root)
    {
        SafeJobHandle jobHandle = Unwrap<SafeJobHandle>(job.NativeHandle);
        NativeProcessState state = Unwrap<NativeProcessState>(root.NativeHandle);

        if (!NativeMethods.GetExitCodeProcess(state.Process, out uint exitCode))
        {
            throw LastWin32("GetExitCodeProcess");
        }

        if (exitCode != NativeMethods.StillActive)
        {
            return false;
        }

        // Detects PID reuse: if the OS has recycled this PID for an unrelated process since
        // LaunchSuspendedRoot cached the original creation time, the two will differ.
        if (GetCreationTime(state.Process) != state.KernelCreationUtc)
        {
            return false;
        }

        if (!NativeMethods.IsProcessInJob(state.Process, jobHandle, out bool inJob))
        {
            throw LastWin32("IsProcessInJob");
        }

        return inJob;
    }

    public C012ProcessToken LaunchSuspendedSubmitter(C012JobToken job) => LaunchSuspended(_submitterArguments);

    public void AssignSubmitterToJob(C012JobToken job, C012ProcessToken submitter) => Assign(job, submitter);

    public bool VerifySubmitterSameJob(C012JobToken job, C012ProcessToken submitter)
    {
        SafeJobHandle jobHandle = Unwrap<SafeJobHandle>(job.NativeHandle);
        NativeProcessState state = Unwrap<NativeProcessState>(submitter.NativeHandle);
        if (!NativeMethods.IsProcessInJob(state.Process, jobHandle, out bool inJob))
        {
            throw LastWin32("IsProcessInJob");
        }

        return inJob;
    }

    public void ResumeAndAwaitSubmitter(C012JobToken job, C012ProcessToken submitter)
    {
        NativeProcessState state = Unwrap<NativeProcessState>(submitter.NativeHandle);
        Resume(submitter);

        try
        {
            uint waitResult = NativeMethods.WaitForSingleObject(
                state.Process, checked((uint)_submitterWaitTimeout.TotalMilliseconds));
            if (waitResult == NativeMethods.WaitTimeout)
            {
                throw new InvalidOperationException("The C2 submitter did not exit within the configured timeout.");
            }

            if (waitResult != NativeMethods.WaitObject0)
            {
                throw LastWin32("WaitForSingleObject");
            }
        }
        finally
        {
            // The submitter is a disposable, transient job member by contract
            // (C2_CONFIG_SUBMITTER_SAME_JOB_ONLY): our own handle reference to it is never
            // needed again after this wait, whichever way the wait ends -- TeardownJob's
            // TerminateJobObject reaches it through the Job, not through this handle.
            state.Process.Dispose();
        }
    }

    public void TeardownJob(C012JobToken job)
    {
        SafeJobHandle jobHandle = Unwrap<SafeJobHandle>(job.NativeHandle);
        try
        {
            if (!jobHandle.IsClosed && !jobHandle.IsInvalid)
            {
                NativeMethods.TerminateJobObject(jobHandle, TeardownExitCode);
            }
        }
        catch (ObjectDisposedException)
        {
            // Concurrent close of a kill-on-close handle already achieves the desired outcome.
        }
        finally
        {
            jobHandle.Dispose();
            _rootState?.Process.Dispose();
            _rootState = null;
        }
    }

    // Test-only escape hatch: closes the Job's own handle directly, without going through
    // TerminateJobObject or TeardownJob's guard against a second call -- exactly what
    // happens at the OS level whether the last handle closes via an explicit Dispose or
    // because the owning process was killed. Lets a Windows-only smoke test verify
    // KILL_ON_JOB_CLOSE ("last Job handle close", not a simulated host crash) without
    // spawning and killing a second real process. Never called by
    // C012OrchestratingProcessor or C012HostCli.
    public static void CloseJobHandleForTesting(C012JobToken job)
    {
        ArgumentNullException.ThrowIfNull(job);
        Unwrap<SafeJobHandle>(job.NativeHandle).Dispose();
    }

    private C012ProcessToken LaunchSuspended(IReadOnlyList<string> arguments)
    {
        using TargetExecutableLease lease = TargetExecutableLease.Open(_executablePath);
        if (!lease.MatchesExpectedSha256(_expectedSha256Hex))
        {
            throw new InvalidOperationException(
                "The executable's on-disk SHA-256 no longer matches the pinned value; refusing to launch.");
        }

        ProcessCreationFlags flags = ProcessCreationFlags.CreateSuspended
            | ProcessCreationFlags.CreateUnicodeEnvironment
            | ProcessCreationFlags.CreateNoWindow;

        using EnvironmentBlock environment = EnvironmentBlock.CreateAllowlisted();
        var startup = new StartupInfo { Cb = checked((uint)Marshal.SizeOf<StartupInfo>()) };
        var commandLine = new StringBuilder(WindowsCommandLine.Build(_executablePath, arguments));
        string workingDirectory = Path.GetDirectoryName(_executablePath)
            ?? throw new InvalidOperationException("The executable path has no parent directory.");

        bool created = NativeMethods.CreateProcess(
            _executablePath,
            commandLine,
            IntPtr.Zero,
            IntPtr.Zero,
            inheritHandles: false,
            flags,
            environment.Pointer,
            workingDirectory,
            ref startup,
            out ProcessInformation nativeProcess);

        if (!created)
        {
            throw LastWin32("CreateProcessW");
        }

        var process = new SafeKernelObjectHandle(nativeProcess.Process, ownsHandle: true);
        var thread = new SafeKernelObjectHandle(nativeProcess.Thread, ownsHandle: true);
        try
        {
            var state = new NativeProcessState(process, thread, nativeProcess.ProcessId, GetCreationTime(process));
            return new C012ProcessToken(state);
        }
        catch
        {
            // A step after CreateProcess failed before a token could be returned to the
            // caller: no C012ProcessToken exists yet for this process, so nothing else will
            // ever learn about it. This method alone is responsible for not leaking it.
            NativeMethods.TerminateProcess(process, FailureExitCode);
            thread.Dispose();
            process.Dispose();
            throw;
        }
    }

    private static void Assign(C012JobToken job, C012ProcessToken token)
    {
        SafeJobHandle jobHandle = Unwrap<SafeJobHandle>(job.NativeHandle);
        NativeProcessState state = Unwrap<NativeProcessState>(token.NativeHandle);

        if (!NativeMethods.AssignProcessToJobObject(jobHandle, state.Process))
        {
            int assignError = Marshal.GetLastWin32Error();
            // The process has never run (still suspended) and was never made a Job member:
            // TeardownJob (which only terminates the Job) cannot reach it, so this method
            // must clean it up itself before propagating the failure.
            NativeMethods.TerminateProcess(state.Process, FailureExitCode);
            state.Thread?.Dispose();
            state.Process.Dispose();
            throw new Win32Exception(assignError, "AssignProcessToJobObject failed.");
        }
    }

    private static void Resume(C012ProcessToken token)
    {
        NativeProcessState state = Unwrap<NativeProcessState>(token.NativeHandle);
        SafeKernelObjectHandle thread = state.Thread
            ?? throw new InvalidOperationException("This process token has already been resumed.");

        uint previousSuspendCount = NativeMethods.ResumeThread(thread);
        if (previousSuspendCount == uint.MaxValue)
        {
            throw LastWin32("ResumeThread");
        }

        if (previousSuspendCount != 1)
        {
            throw new InvalidOperationException("The primary thread suspend count was not exactly one.");
        }

        thread.Dispose();
        state.Thread = null;
    }

    private static void ConfigureAndVerifyKillOnJobClose(SafeJobHandle job)
    {
        var requested = new JobObjectExtendedLimitInformation
        {
            BasicLimitInformation = new JobObjectBasicLimitInformation
            {
                LimitFlags = JobObjectLimitFlags.KillOnJobClose,
            },
        };

        uint size = checked((uint)Marshal.SizeOf<JobObjectExtendedLimitInformation>());
        if (!NativeMethods.SetInformationJobObject(
                job, JobObjectInformationClass.ExtendedLimitInformation, ref requested, size))
        {
            throw LastWin32("SetInformationJobObject");
        }

        if (!NativeMethods.QueryInformationJobObject(
                job,
                JobObjectInformationClass.ExtendedLimitInformation,
                out JobObjectExtendedLimitInformation actual,
                size,
                out _))
        {
            throw LastWin32("QueryInformationJobObject");
        }

        JobObjectLimitFlags flags = actual.BasicLimitInformation.LimitFlags;
        bool killOnJobCloseVerified = flags.HasFlag(JobObjectLimitFlags.KillOnJobClose);
        bool breakawayAllowed = flags.HasFlag(JobObjectLimitFlags.BreakawayOk);
        bool silentBreakawayAllowed = flags.HasFlag(JobObjectLimitFlags.SilentBreakawayOk);

        if (!killOnJobCloseVerified || breakawayAllowed || silentBreakawayAllowed)
        {
            throw new InvalidOperationException("The Job Object KILL_ON_JOB_CLOSE policy could not be verified.");
        }
    }

    private static DateTimeOffset GetCreationTime(SafeKernelObjectHandle process)
    {
        if (!NativeMethods.GetProcessTimes(process, out FILETIME creation, out _, out _, out _))
        {
            throw LastWin32("GetProcessTimes");
        }

        long fileTime = ((long)creation.dwHighDateTime << 32) | (uint)creation.dwLowDateTime;
        return new DateTimeOffset(DateTime.FromFileTimeUtc(fileTime));
    }

    private static T Unwrap<T>(object nativeHandle) where T : class
    {
        if (nativeHandle is not T typed)
        {
            throw new InvalidOperationException(
                $"Expected a token produced by {nameof(C012InnocuousRootProcessLauncher)}, got {nativeHandle.GetType().Name}.");
        }

        return typed;
    }

    private static Win32Exception LastWin32(string operation) =>
        new(Marshal.GetLastWin32Error(), $"{operation} failed.");

    private sealed class NativeProcessState
    {
        public NativeProcessState(
            SafeKernelObjectHandle process, SafeKernelObjectHandle thread, uint processId, DateTimeOffset kernelCreationUtc)
        {
            Process = process;
            Thread = thread;
            ProcessId = processId;
            KernelCreationUtc = kernelCreationUtc;
        }

        public SafeKernelObjectHandle Process { get; }

        public SafeKernelObjectHandle? Thread { get; set; }

        public uint ProcessId { get; }

        public DateTimeOffset KernelCreationUtc { get; }
    }
}

using System.ComponentModel;
using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;
using System.Text;

namespace TradeJournal.Lab.JobHarness;

internal sealed record JobRunResult(int ExitCode);

internal static class JobObjectRunner
{
    private const uint FailureExitCode = 125;
    private const uint TimeoutExitCode = 124;
    private const uint CancelExitCode = 130;
    private const int PollMilliseconds = 50;
    private const int StopReasonNone = 0;
    private const int StopReasonCancellation = 1;
    private const int StopReasonTimeout = 2;

    public static JobRunResult Run(LaunchOptions options, HarnessMetadata metadata, MetadataWriter writer)
    {
        ArgumentNullException.ThrowIfNull(options);
        ArgumentNullException.ThrowIfNull(metadata);
        ArgumentNullException.ThrowIfNull(writer);

        using SafeJobHandle job = NativeMethods.CreateJobObject(IntPtr.Zero, name: null);
        if (job.IsInvalid)
        {
            throw LastWin32("CreateJobObjectW");
        }

        ConfigureAndVerifyJob(job, metadata.JobPolicy);

        ProcessInformation nativeProcess = default;
        TimeSpan? watchdogTimeout = options.Timeout;
        SafeKernelObjectHandle? process = null;
        SafeKernelObjectHandle? thread = null;
        bool assigned = false;
        bool resumed = false;
        bool jobEmpty = false;
        int stopReason = StopReasonNone;
        bool lifecycleFinished = false;
        var lifecycleGate = new object();
        Timer? timeoutWatchdog = null;

        if (watchdogTimeout is not null)
        {
            timeoutWatchdog = new Timer(
                _ =>
                {
                    lock (lifecycleGate)
                    {
                        if (lifecycleFinished || !resumed)
                        {
                            return;
                        }

                        try
                        {
                            JobObjectBasicAccountingInformation accounting = QueryAccounting(job);
                            if (accounting.ActiveProcesses == 0)
                            {
                                // The one-shot timer lost the race against natural completion.
                                // Mark completion without ever terminating an already-empty job.
                                lifecycleFinished = true;
                                jobEmpty = true;
                                return;
                            }

                            if (Interlocked.CompareExchange(
                                    ref stopReason,
                                    StopReasonTimeout,
                                    StopReasonNone) == StopReasonNone)
                            {
                                TryTerminateJob(job, TimeoutExitCode);
                            }
                        }
                        catch (Exception exception) when (exception is Win32Exception or InvalidOperationException)
                        {
                            // A watchdog callback must never throw on the ThreadPool. A failed
                            // accounting query is handled fail-closed as a timeout condition.
                            if (Interlocked.CompareExchange(
                                    ref stopReason,
                                    StopReasonTimeout,
                                    StopReasonNone) == StopReasonNone)
                            {
                                TryTerminateJob(job, TimeoutExitCode);
                            }
                        }
                    }
                },
                state: null,
                dueTime: Timeout.InfiniteTimeSpan,
                period: Timeout.InfiniteTimeSpan);
        }

        ConsoleCancelEventHandler cancelHandler = (_, eventArgs) =>
        {
            eventArgs.Cancel = true;
            lock (lifecycleGate)
            {
                if (lifecycleFinished)
                {
                    return;
                }

                if (assigned)
                {
                    try
                    {
                        JobObjectBasicAccountingInformation accounting = QueryAccounting(job);
                        if (accounting.ActiveProcesses == 0)
                        {
                            lifecycleFinished = true;
                            jobEmpty = true;
                            timeoutWatchdog?.Change(Timeout.InfiniteTimeSpan, Timeout.InfiniteTimeSpan);
                            return;
                        }
                    }
                    catch (Exception exception) when (exception is Win32Exception or InvalidOperationException)
                    {
                        // TerminateJobObject/handle close remain the fail-closed fallback.
                    }
                }

                if (Interlocked.CompareExchange(
                        ref stopReason,
                        StopReasonCancellation,
                        StopReasonNone) != StopReasonNone)
                {
                    return;
                }

                timeoutWatchdog?.Change(Timeout.InfiniteTimeSpan, Timeout.InfiniteTimeSpan);
                if (assigned)
                {
                    TryTerminateJob(job, CancelExitCode);
                }
            }
        };

        bool cancelHandlerRegistered = false;
        try
        {
            Console.CancelKeyPress += cancelHandler;
            cancelHandlerRegistered = true;

            if (Volatile.Read(ref stopReason) == StopReasonCancellation)
            {
                return CompleteBeforeResumeCancellation(metadata, writer);
            }

            ProcessCreationFlags flags =
                ProcessCreationFlags.CreateSuspended | ProcessCreationFlags.CreateUnicodeEnvironment;

            if (options.NoWindow)
            {
                flags |= ProcessCreationFlags.CreateNoWindow;
            }

            using EnvironmentBlock environment = EnvironmentBlock.CreateAllowlisted();

            var startup = new StartupInfo
            {
                Cb = checked((uint)Marshal.SizeOf<StartupInfo>()),
            };

            var commandLine = new StringBuilder(WindowsCommandLine.Build(options.ExecutablePath, options.Arguments));
            bool created = NativeMethods.CreateProcess(
                options.ExecutablePath,
                commandLine,
                IntPtr.Zero,
                IntPtr.Zero,
                inheritHandles: false,
                flags,
                environment.Pointer,
                options.WorkingDirectory,
                ref startup,
                out nativeProcess);

            if (!created)
            {
                throw LastWin32("CreateProcessW");
            }

            process = new SafeKernelObjectHandle(nativeProcess.Process, ownsHandle: true);
            thread = new SafeKernelObjectHandle(nativeProcess.Thread, ownsHandle: true);

            metadata.Process.RootPid = nativeProcess.ProcessId;
            metadata.Process.PrimaryThreadId = nativeProcess.ThreadId;
            metadata.Process.CreatedSuspended = true;
            metadata.Process.KernelCreationUtc = GetCreationTime(process);
            metadata.ProcessCreatedUtc = DateTimeOffset.UtcNow;
            if (!NativeMethods.ProcessIdToSessionId(nativeProcess.ProcessId, out uint sessionId))
            {
                throw LastWin32("ProcessIdToSessionId");
            }

            metadata.Process.WindowsSessionId = sessionId;

            if (!NativeMethods.AssignProcessToJobObject(job, process))
            {
                int assignError = Marshal.GetLastWin32Error();
                // The primary thread has never run. Explicitly terminate the unassigned,
                // suspended process because KILL_ON_JOB_CLOSE cannot cover it.
                NativeMethods.TerminateProcess(process, FailureExitCode);
                uint assignCleanupWait = NativeMethods.WaitForSingleObject(process, 5000);
                if (assignCleanupWait == NativeMethods.WaitTimeout)
                {
                    throw new InvalidOperationException("AssignProcessToJobObject cleanup wait timed out.");
                }

                if (assignCleanupWait == NativeMethods.WaitFailed)
                {
                    throw LastWin32("WaitForSingleObject");
                }

                if (assignCleanupWait != NativeMethods.WaitObject0)
                {
                    throw new InvalidOperationException(
                        $"Unexpected AssignProcessToJobObject cleanup wait result: {assignCleanupWait}.");
                }
                throw new Win32Exception(assignError, "AssignProcessToJobObject failed.");
            }

            lock (lifecycleGate)
            {
                assigned = true;
            }
            metadata.Process.AssignedBeforeResume = true;
            metadata.AssignedUtc = DateTimeOffset.UtcNow;
            metadata.Status = HarnessStatus.AssignedSuspended;

            // Evidence persistence is a precondition for executing any target instruction.
            // A write failure here exits the scope and closes the job while still suspended.
            writer.Write(metadata);

            if (Volatile.Read(ref stopReason) == StopReasonCancellation)
            {
                TryTerminateJob(job, CancelExitCode);
                WaitForJobToDrain(job, TimeSpan.FromSeconds(5));
                JobObjectBasicAccountingInformation cancellationAccounting = QueryAccounting(job);
                jobEmpty = cancellationAccounting.ActiveProcesses == 0;
                metadata.ProcessResult.JobTotalProcesses = cancellationAccounting.TotalProcesses;
                metadata.ProcessResult.JobTotalTerminatedProcesses = cancellationAccounting.TotalTerminatedProcesses;
                return CompleteBeforeResumeCancellation(metadata, writer);
            }

            uint previousSuspendCount;
            lock (lifecycleGate)
            {
                if (Volatile.Read(ref stopReason) == StopReasonCancellation)
                {
                    previousSuspendCount = uint.MaxValue;
                }
                else
                {
                    if (timeoutWatchdog is not null
                        && !timeoutWatchdog.Change(
                            watchdogTimeout ?? throw new InvalidOperationException(
                                "Watchdog was initialized without a timeout."),
                            Timeout.InfiniteTimeSpan))
                    {
                        throw new InvalidOperationException("The timeout watchdog could not be armed.");
                    }

                    previousSuspendCount = NativeMethods.ResumeThread(thread);
                    if (previousSuspendCount == uint.MaxValue)
                    {
                        timeoutWatchdog?.Change(Timeout.InfiniteTimeSpan, Timeout.InfiniteTimeSpan);
                        throw LastWin32("ResumeThread");
                    }

                    resumed = true;
                }
            }

            if (Volatile.Read(ref stopReason) == StopReasonCancellation)
            {
                TryTerminateJob(job, CancelExitCode);
                WaitForJobToDrain(job, TimeSpan.FromSeconds(5));
                JobObjectBasicAccountingInformation cancellationAccounting = QueryAccounting(job);
                jobEmpty = cancellationAccounting.ActiveProcesses == 0;
                metadata.ProcessResult.JobTotalProcesses = cancellationAccounting.TotalProcesses;
                metadata.ProcessResult.JobTotalTerminatedProcesses = cancellationAccounting.TotalTerminatedProcesses;
                return CompleteBeforeResumeCancellation(metadata, writer);
            }

            metadata.Process.ResumePreviousSuspendCount = previousSuspendCount;
            if (previousSuspendCount != 1)
            {
                throw new InvalidOperationException("The primary thread suspend count was not exactly one.");
            }

            metadata.ResumedUtc = DateTimeOffset.UtcNow;
            metadata.Status = HarnessStatus.Running;
            writer.Write(metadata);

            JobObjectBasicAccountingInformation finalAccounting = default;
            while (true)
            {
                finalAccounting = QueryAccounting(job);
                int observedStopReason = Volatile.Read(ref stopReason);
                if (observedStopReason == StopReasonCancellation)
                {
                    metadata.ProcessResult.Cancelled = true;
                    metadata.Status = HarnessStatus.Cancelled;
                    TryTerminateJob(job, CancelExitCode);
                    WaitForJobToDrain(job, TimeSpan.FromSeconds(5));
                    finalAccounting = QueryAccounting(job);
                    jobEmpty = finalAccounting.ActiveProcesses == 0;
                    lock (lifecycleGate)
                    {
                        lifecycleFinished = true;
                        timeoutWatchdog?.Change(Timeout.InfiniteTimeSpan, Timeout.InfiniteTimeSpan);
                    }
                    break;
                }

                if (observedStopReason == StopReasonTimeout)
                {
                    metadata.ProcessResult.TimedOut = true;
                    metadata.Status = HarnessStatus.TimedOut;
                    TryTerminateJob(job, TimeoutExitCode);
                    WaitForJobToDrain(job, TimeSpan.FromSeconds(5));
                    finalAccounting = QueryAccounting(job);
                    jobEmpty = finalAccounting.ActiveProcesses == 0;
                    lock (lifecycleGate)
                    {
                        lifecycleFinished = true;
                        timeoutWatchdog?.Change(Timeout.InfiniteTimeSpan, Timeout.InfiniteTimeSpan);
                    }
                    break;
                }

                if (finalAccounting.ActiveProcesses == 0)
                {
                    bool completedNaturally;
                    lock (lifecycleGate)
                    {
                        if (Volatile.Read(ref stopReason) != StopReasonNone)
                        {
                            completedNaturally = false;
                        }
                        else
                        {
                            finalAccounting = QueryAccounting(job);
                            completedNaturally = finalAccounting.ActiveProcesses == 0;
                            if (completedNaturally)
                            {
                                lifecycleFinished = true;
                                jobEmpty = true;
                                timeoutWatchdog?.Change(Timeout.InfiniteTimeSpan, Timeout.InfiniteTimeSpan);
                            }
                        }
                    }

                    if (completedNaturally)
                    {
                        break;
                    }

                    continue;
                }

                Thread.Sleep(PollMilliseconds);
            }

            metadata.ProcessResult.JobTotalProcesses = finalAccounting.TotalProcesses;
            metadata.ProcessResult.JobTotalTerminatedProcesses = finalAccounting.TotalTerminatedProcesses;

            uint waitResult = NativeMethods.WaitForSingleObject(process, 5000);
            if (waitResult is not NativeMethods.WaitObject0 and not NativeMethods.WaitTimeout)
            {
                throw LastWin32("WaitForSingleObject");
            }

            if (!NativeMethods.GetExitCodeProcess(process, out uint primaryExitCode))
            {
                throw LastWin32("GetExitCodeProcess");
            }

            metadata.ProcessResult.PrimaryExitCode = primaryExitCode;

            metadata.CompletedUtc = DateTimeOffset.UtcNow;
            int exitCode;
            if (metadata.ProcessResult.Cancelled)
            {
                exitCode = checked((int)CancelExitCode);
            }
            else if (metadata.ProcessResult.TimedOut)
            {
                exitCode = checked((int)TimeoutExitCode);
            }
            else
            {
                exitCode = PrimaryExitCodeToManaged(metadata.ProcessResult.PrimaryExitCode);
                metadata.Status = HarnessStatus.Completed;
            }

            writer.Write(metadata);
            return new JobRunResult(exitCode);
        }
        finally
        {
            if (cancelHandlerRegistered)
            {
                Console.CancelKeyPress -= cancelHandler;
            }

            lock (lifecycleGate)
            {
                lifecycleFinished = true;
                timeoutWatchdog?.Change(Timeout.InfiniteTimeSpan, Timeout.InfiniteTimeSpan);
            }

            DisposeWatchdogAndWait(timeoutWatchdog);

            if (process is not null && !assigned)
            {
                NativeMethods.TerminateProcess(process, FailureExitCode);
            }

            if (assigned && resumed && !jobEmpty)
            {
                TryTerminateJob(job, FailureExitCode);
            }

            thread?.Dispose();
            process?.Dispose();
            // Disposing job is the final fail-closed boundary. Because the verified job has
            // KILL_ON_JOB_CLOSE and no breakaway flags, every remaining descendant is killed.
        }
    }

    private static void ConfigureAndVerifyJob(SafeJobHandle job, JobPolicyMetadata metadata)
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
                job,
                JobObjectInformationClass.ExtendedLimitInformation,
                ref requested,
                size))
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
        metadata.KillOnJobCloseVerified = flags.HasFlag(JobObjectLimitFlags.KillOnJobClose);
        metadata.BreakawayAllowed = flags.HasFlag(JobObjectLimitFlags.BreakawayOk);
        metadata.SilentBreakawayAllowed = flags.HasFlag(JobObjectLimitFlags.SilentBreakawayOk);

        if (metadata.KillOnJobCloseVerified != true
            || metadata.BreakawayAllowed != false
            || metadata.SilentBreakawayAllowed != false)
        {
            throw new InvalidOperationException("The Job Object policy could not be verified.");
        }
    }

    private static JobObjectBasicAccountingInformation QueryAccounting(SafeJobHandle job)
    {
        uint size = checked((uint)Marshal.SizeOf<JobObjectBasicAccountingInformation>());
        if (!NativeMethods.QueryInformationJobObject(
                job,
                JobObjectInformationClass.BasicAccountingInformation,
                out JobObjectBasicAccountingInformation information,
                size,
                out _))
        {
            throw LastWin32("QueryInformationJobObject");
        }

        return information;
    }

    private static void WaitForJobToDrain(SafeJobHandle job, TimeSpan limit)
    {
        Stopwatch timer = Stopwatch.StartNew();
        while (timer.Elapsed < limit)
        {
            if (QueryAccounting(job).ActiveProcesses == 0)
            {
                return;
            }

            Thread.Sleep(PollMilliseconds);
        }
    }

    private static DateTimeOffset GetCreationTime(SafeKernelObjectHandle process)
    {
        if (!NativeMethods.GetProcessTimes(
                process,
                out FILETIME creation,
                out _,
                out _,
                out _))
        {
            throw LastWin32("GetProcessTimes");
        }

        long fileTime = ((long)creation.dwHighDateTime << 32) | (uint)creation.dwLowDateTime;
        return new DateTimeOffset(DateTime.FromFileTimeUtc(fileTime));
    }

    private static int PrimaryExitCodeToManaged(uint? exitCode)
    {
        if (exitCode is null || exitCode == NativeMethods.StillActive)
        {
            throw new InvalidOperationException("The primary process did not expose a final exit code.");
        }

        // Windows stores process exit status as uint; .NET exposes an int exit code.
        return unchecked((int)exitCode.Value);
    }

    private static void DisposeWatchdogAndWait(Timer? timeoutWatchdog)
    {
        if (timeoutWatchdog is not null)
        {
            timeoutWatchdog.DisposeAsync().AsTask().GetAwaiter().GetResult();
        }
    }

    private static JobRunResult CompleteBeforeResumeCancellation(
        HarnessMetadata metadata,
        MetadataWriter writer)
    {
        metadata.ProcessResult.Cancelled = true;
        metadata.Status = HarnessStatus.Cancelled;
        metadata.CompletedUtc = DateTimeOffset.UtcNow;
        writer.Write(metadata);
        return new JobRunResult(checked((int)CancelExitCode));
    }

    private static void TryTerminateJob(SafeJobHandle job, uint exitCode)
    {
        try
        {
            if (!job.IsClosed && !job.IsInvalid)
            {
                NativeMethods.TerminateJobObject(job, exitCode);
            }
        }
        catch (ObjectDisposedException)
        {
            // Closing a kill-on-close handle concurrently is already the desired outcome.
        }
    }

    private static Win32Exception LastWin32(string operation) =>
        new(Marshal.GetLastWin32Error(), $"{operation} failed.");
}

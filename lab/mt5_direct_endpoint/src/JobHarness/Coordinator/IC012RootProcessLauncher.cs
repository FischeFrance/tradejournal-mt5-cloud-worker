namespace TradeJournal.Lab.JobHarness.Coordinator;

// Opaque wrapper so C012OrchestratingProcessor and IC012RootProcessLauncher never depend on
// a concrete handle type: a fake test double wraps a dummy object; a future real,
// Windows-only implementation would wrap a SafeJobHandle. The orchestrator only ever passes
// a token back to the same launcher instance that produced it.
public sealed class C012JobToken
{
    public C012JobToken(object nativeHandle)
    {
        ArgumentNullException.ThrowIfNull(nativeHandle);
        NativeHandle = nativeHandle;
    }

    public object NativeHandle { get; }
}

public sealed class C012ProcessToken
{
    public C012ProcessToken(object nativeHandle)
    {
        ArgumentNullException.ThrowIfNull(nativeHandle);
        NativeHandle = nativeHandle;
    }

    public object NativeHandle { get; }
}

// Every real-world side effect C012OrchestratingProcessor needs, one method per FSM
// sub-transition. Implementations are expected to throw on failure (matching this
// codebase's existing Win32-wrapper convention, e.g. JobObjectRunner). The two Verify*
// methods return false for a completed, successful check that determined the answer is
// "no" -- that is a different, more specific outcome than the check itself failing to run.
public interface IC012RootProcessLauncher
{
    C012JobToken CreateJob();

    C012ProcessToken LaunchSuspendedRoot(C012JobToken job);

    void AssignRootToJob(C012JobToken job, C012ProcessToken root);

    void ResumeRoot(C012ProcessToken root);

    bool VerifyRootAlive(C012JobToken job, C012ProcessToken root);

    C012ProcessToken LaunchSuspendedSubmitter(C012JobToken job);

    void AssignSubmitterToJob(C012JobToken job, C012ProcessToken submitter);

    bool VerifySubmitterSameJob(C012JobToken job, C012ProcessToken submitter);

    void ResumeAndAwaitSubmitter(C012JobToken job, C012ProcessToken submitter);

    void TeardownJob(C012JobToken job);
}

namespace TradeJournal.Lab.JobHarness.Coordinator;

// Placeholder IC012RootProcessLauncher for B4.2: every operation fails immediately and
// deterministically, without ever calling a Win32 API, touching a Job Object, or starting
// any process (MT5 or otherwise) -- this class contains no P/Invoke, no NativeMethods
// reference, and no Process.Start call at all. c012-host start uses this until a real
// (later phase) or fake (test-only) launcher replaces it. With this launcher, C0 always and
// only ever reaches FailedClosed via OperationFailed: a fully valid, deterministic outcome
// that exercises the real Named Pipe transport end to end without any native side effect.
public sealed class C012NotImplementedRootProcessLauncher : IC012RootProcessLauncher
{
    public C012JobToken CreateJob() => throw NotImplemented(nameof(CreateJob));

    public C012ProcessToken LaunchSuspendedRoot(C012JobToken job) => throw NotImplemented(nameof(LaunchSuspendedRoot));

    public void AssignRootToJob(C012JobToken job, C012ProcessToken root) => throw NotImplemented(nameof(AssignRootToJob));

    public void ResumeRoot(C012ProcessToken root) => throw NotImplemented(nameof(ResumeRoot));

    public bool VerifyRootAlive(C012JobToken job, C012ProcessToken root) => throw NotImplemented(nameof(VerifyRootAlive));

    public C012ProcessToken LaunchSuspendedSubmitter(C012JobToken job) => throw NotImplemented(nameof(LaunchSuspendedSubmitter));

    public void AssignSubmitterToJob(C012JobToken job, C012ProcessToken submitter) => throw NotImplemented(nameof(AssignSubmitterToJob));

    public bool VerifySubmitterSameJob(C012JobToken job, C012ProcessToken submitter) => throw NotImplemented(nameof(VerifySubmitterSameJob));

    public void ResumeAndAwaitSubmitter(C012JobToken job, C012ProcessToken submitter) => throw NotImplemented(nameof(ResumeAndAwaitSubmitter));

    public void TeardownJob(C012JobToken job) => throw NotImplemented(nameof(TeardownJob));

    private static InvalidOperationException NotImplemented(string operation) =>
        new($"{operation} is not implemented in this build: the real Job Object launcher has not landed yet.");
}

namespace TradeJournal.Lab.JobHarness.Coordinator;

// Wraps a C012RequestSequencer with real side-effect orchestration. A client trigger
// (BeginC0/BeginC1/BeginC2) is applied only through the sequencer's own gate -- session id,
// sequence number, legal transition, all unchanged from B2/B3 -- and a real operation is
// attempted only once that gate has accepted the request; an illegal or unauthenticated
// request never reaches the launcher. The FSM is promoted to *Retained/Teardown only after
// the corresponding real operation has fully succeeded. Any failure, partial or total, is
// applied as OperationFailed (or RootProcessDied, for a completed liveness check that
// determined the root is specifically gone) and triggers best-effort cleanup, at most once.
public sealed class C012OrchestratingProcessor : IC012RequestProcessor
{
    private readonly C012RequestSequencer _sequencer;
    private readonly IC012RootProcessLauncher _launcher;
    private C012JobToken? _job;
    private C012ProcessToken? _root;
    private bool _teardownAttempted;

    public C012OrchestratingProcessor(C012RequestSequencer sequencer, IC012RootProcessLauncher launcher)
    {
        ArgumentNullException.ThrowIfNull(sequencer);
        ArgumentNullException.ThrowIfNull(launcher);
        _sequencer = sequencer;
        _launcher = launcher;
    }

    public C012TransitionResult Apply(C012RequestEnvelope request)
    {
        ArgumentNullException.ThrowIfNull(request);

        C012TransitionResult gateResult = _sequencer.Apply(request);
        if (!gateResult.Accepted)
        {
            return gateResult;
        }

        return (request.Control, request.RequestType) switch
        {
            (C012Control.C0, C012RequestType.Start) => RunC0(),
            (C012Control.C1, C012RequestType.Query) => RunC1(),
            (C012Control.C2, C012RequestType.SubmitConfig) => RunC2(),
            _ => gateResult,
        };
    }

    // Local variables (not the _job/_root fields) are used for every launcher call in this
    // method: a field is reassigned only once each operation has already succeeded, so the
    // fields never need to be read back mid-method, and every launcher call site works with
    // a definitely-non-null local.
    private C012TransitionResult RunC0()
    {
        C012JobToken job;
        try
        {
            job = _launcher.CreateJob();
        }
        catch (Exception)
        {
            return FailOperation();
        }

        _job = job;
        _sequencer.ApplyInternal(C012Trigger.C0JobCreated);

        C012ProcessToken root;
        try
        {
            root = _launcher.LaunchSuspendedRoot(job);
            _launcher.AssignRootToJob(job, root);
            _launcher.ResumeRoot(root);
        }
        catch (Exception)
        {
            return FailOperation();
        }

        _root = root;
        return _sequencer.ApplyInternal(C012Trigger.C0RootLaunched);
    }

    private C012TransitionResult RunC1()
    {
        if (_job is null || _root is null)
        {
            return FailOperation();
        }

        C012JobToken job = _job;
        C012ProcessToken root = _root;

        bool alive;
        try
        {
            alive = _launcher.VerifyRootAlive(job, root);
        }
        catch (Exception)
        {
            return FailOperation();
        }

        if (!alive)
        {
            return FailRootDied();
        }

        return _sequencer.ApplyInternal(C012Trigger.C1DiscoveryComplete);
    }

    private C012TransitionResult RunC2()
    {
        if (_job is null || _root is null)
        {
            return FailOperation();
        }

        C012JobToken job = _job;

        C012ProcessToken submitter;
        try
        {
            submitter = _launcher.LaunchSuspendedSubmitter(job);
            _launcher.AssignSubmitterToJob(job, submitter);
        }
        catch (Exception)
        {
            return FailOperation();
        }

        bool sameJob;
        try
        {
            sameJob = _launcher.VerifySubmitterSameJob(job, submitter);
        }
        catch (Exception)
        {
            return FailOperation();
        }

        if (!sameJob)
        {
            return FailOperation();
        }

        C012TransitionResult assigned = _sequencer.ApplyInternal(C012Trigger.C2SubmitterAssigned);
        if (!assigned.Accepted)
        {
            return FailOperation();
        }

        try
        {
            _launcher.ResumeAndAwaitSubmitter(job, submitter);
        }
        catch (Exception)
        {
            return FailOperation();
        }

        C012TransitionResult loginComplete = _sequencer.ApplyInternal(C012Trigger.C2LoginWindowComplete);
        if (!loginComplete.Accepted)
        {
            return FailOperation();
        }

        if (!TryTeardownReporting())
        {
            return FailOperation();
        }

        return _sequencer.ApplyInternal(C012Trigger.C2TeardownComplete);
    }

    private C012TransitionResult FailOperation()
    {
        _sequencer.ApplyInternal(C012Trigger.OperationFailed);
        TryTeardownReporting();
        return new C012TransitionResult(false, C012State.FailedClosed, C012RejectionReason.OperationFailed);
    }

    private C012TransitionResult FailRootDied()
    {
        _sequencer.ApplyInternal(C012Trigger.RootProcessDied);
        TryTeardownReporting();
        return new C012TransitionResult(false, C012State.FailedClosed, C012RejectionReason.RootProcessDied);
    }

    // Tears down the Job at most once, regardless of whether it is called from the normal
    // C2 completion path or from a failure path -- calling it twice would either be a
    // meaningless no-op or, for a real native implementation, an error on an
    // already-closed handle.
    private bool TryTeardownReporting()
    {
        if (_job is null || _teardownAttempted)
        {
            return true;
        }

        C012JobToken job = _job;
        _teardownAttempted = true;
        try
        {
            _launcher.TeardownJob(job);
            return true;
        }
        catch (Exception)
        {
            return false;
        }
    }
}

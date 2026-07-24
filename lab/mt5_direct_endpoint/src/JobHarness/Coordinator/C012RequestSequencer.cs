namespace TradeJournal.Lab.JobHarness.Coordinator;

// Binds a C012StateMachine to one session id and enforces strictly monotonic request
// sequencing before any trigger reaches the machine. A rejected request never reaches
// C012StateMachine.Apply, so state and the accepted-sequence counter are provably untouched.
public sealed class C012RequestSequencer
{
    private readonly C012StateMachine _machine = new();
    private long _lastAcceptedSequence;

    public C012RequestSequencer(Guid sessionId)
    {
        SessionId = sessionId;
    }

    public Guid SessionId { get; }

    public C012State CurrentState => _machine.CurrentState;

    public C012TransitionResult Apply(C012RequestEnvelope request)
    {
        ArgumentNullException.ThrowIfNull(request);

        if (request.SessionId != SessionId)
        {
            return C012TransitionResult.Reject(_machine.CurrentState, C012RejectionReason.SessionMismatch);
        }

        if (request.SequenceNumber != _lastAcceptedSequence + 1)
        {
            return C012TransitionResult.Reject(_machine.CurrentState, C012RejectionReason.SequenceOutOfOrder);
        }

        C012Trigger? trigger = MapToTrigger(request);
        if (trigger is null)
        {
            return C012TransitionResult.Reject(_machine.CurrentState, C012RejectionReason.InvalidRequest);
        }

        C012TransitionResult result = _machine.Apply(trigger.Value);
        if (result.Accepted)
        {
            _lastAcceptedSequence = request.SequenceNumber;
        }

        return result;
    }

    // For host-internal progress/failure signals only (job/root progress, Timeout,
    // RootProcessDied): never client-originated, never sequence-numbered.
    public C012TransitionResult ApplyInternal(C012Trigger trigger)
    {
        if (trigger is C012Trigger.BeginC0 or C012Trigger.BeginC1 or C012Trigger.BeginC2)
        {
            throw new ArgumentException(
                $"{trigger} is client-originated and must go through {nameof(Apply)}({nameof(C012RequestEnvelope)}).",
                nameof(trigger));
        }

        return _machine.Apply(trigger);
    }

    private static C012Trigger? MapToTrigger(C012RequestEnvelope request) => (request.Control, request.RequestType) switch
    {
        (C012Control.C0, C012RequestType.Start) => C012Trigger.BeginC0,
        (C012Control.C1, C012RequestType.Query) => C012Trigger.BeginC1,
        (C012Control.C2, C012RequestType.SubmitConfig) => C012Trigger.BeginC2,
        _ => null,
    };
}

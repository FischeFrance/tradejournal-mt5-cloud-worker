namespace TradeJournal.Lab.JobHarness.Coordinator;

// C012_SINGLE_PROCESS_SESSION lifecycle: C0 LAUNCH_RETAIN, C1 REUSE_RETAIN,
// C2 REUSE_CONFIG_SUBMIT_TEARDOWN. The *Creating/*Launching/*Running/*Submitting sub-states
// track host-internal progress within a control so a mid-control failure leaves an accurate
// state instead of collapsing straight to FailedClosed.
public enum C012State
{
    NotStarted,
    C0JobCreating,
    C0RootLaunching,
    C0Retained,
    C1DiscoveryRunning,
    C1Retained,
    C2Submitting,
    C2LoginWindow,
    C2Teardown,
    Terminated,
    FailedClosed,
}

// BeginC0/BeginC1/BeginC2 are the only triggers that may originate from a sequence-gated
// client request (see C012RequestSequencer). The rest are host-internal progress or failure
// signals, applied directly and never sequence-numbered.
public enum C012Trigger
{
    BeginC0,
    C0JobCreated,
    C0RootLaunched,
    BeginC1,
    C1DiscoveryComplete,
    BeginC2,
    C2SubmitterAssigned,
    C2LoginWindowComplete,
    C2TeardownComplete,
    Timeout,
    RootProcessDied,
}

public enum C012RejectionReason
{
    None,
    IllegalTransition,
    TerminalState,
    SessionMismatch,
    SequenceOutOfOrder,
    InvalidRequest,
}

public sealed record C012Transition(C012State From, C012Trigger Trigger, C012State To);

public sealed record C012TransitionResult(bool Accepted, C012State ResultingState, C012RejectionReason Reason)
{
    public static C012TransitionResult Accept(C012State resultingState) =>
        new(true, resultingState, C012RejectionReason.None);

    public static C012TransitionResult Reject(C012State currentState, C012RejectionReason reason) =>
        new(false, currentState, reason);
}

public enum C012Control
{
    C0,
    C1,
    C2,
}

// C1's single Query covers the whole REUSE_RETAIN discovery step (negative label search then
// exact search) as one host-internal operation, not two separate client requests.
public enum C012RequestType
{
    Start,
    Query,
    SubmitConfig,
}

// Transport-less request envelope. Wire framing, schema versioning and HMAC/session-token
// authentication are deferred to a later patch.
public sealed record C012RequestEnvelope(
    Guid SessionId,
    long SequenceNumber,
    C012Control Control,
    C012RequestType RequestType);

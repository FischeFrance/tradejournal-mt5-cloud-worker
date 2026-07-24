namespace TradeJournal.Lab.JobHarness.Coordinator;

// Pure in-memory FSM for one C012 session. No I/O, no Win32 calls, no IPC. The transition
// table is the single source of truth: anything not in it is refused without mutating state.
public sealed class C012StateMachine
{
    private static readonly IReadOnlyList<C012Transition> HappyPath = new[]
    {
        new C012Transition(C012State.NotStarted, C012Trigger.BeginC0, C012State.C0JobCreating),
        new C012Transition(C012State.C0JobCreating, C012Trigger.C0JobCreated, C012State.C0RootLaunching),
        new C012Transition(C012State.C0RootLaunching, C012Trigger.C0RootLaunched, C012State.C0Retained),
        new C012Transition(C012State.C0Retained, C012Trigger.BeginC1, C012State.C1DiscoveryRunning),
        new C012Transition(C012State.C1DiscoveryRunning, C012Trigger.C1DiscoveryComplete, C012State.C1Retained),
        new C012Transition(C012State.C1Retained, C012Trigger.BeginC2, C012State.C2Submitting),
        new C012Transition(C012State.C2Submitting, C012Trigger.C2SubmitterAssigned, C012State.C2LoginWindow),
        new C012Transition(C012State.C2LoginWindow, C012Trigger.C2LoginWindowComplete, C012State.C2Teardown),
        new C012Transition(C012State.C2Teardown, C012Trigger.C2TeardownComplete, C012State.Terminated),
    };

    public static IReadOnlyCollection<C012State> TerminalStates { get; } =
        new HashSet<C012State> { C012State.Terminated, C012State.FailedClosed };

    // The complete, frozen table: the nine happy-path steps plus, from every non-terminal
    // state, the two universal fail-closed edges (Timeout, RootProcessDied) to FailedClosed.
    public static IReadOnlyList<C012Transition> AllTransitions { get; } = BuildAllTransitions();

    private static readonly Dictionary<(C012State From, C012Trigger Trigger), C012State> Table =
        AllTransitions.ToDictionary(transition => (transition.From, transition.Trigger), transition => transition.To);

    public C012State CurrentState { get; private set; } = C012State.NotStarted;

    public C012TransitionResult Apply(C012Trigger trigger)
    {
        if (TerminalStates.Contains(CurrentState))
        {
            return C012TransitionResult.Reject(CurrentState, C012RejectionReason.TerminalState);
        }

        if (!Table.TryGetValue((CurrentState, trigger), out C012State next))
        {
            return C012TransitionResult.Reject(CurrentState, C012RejectionReason.IllegalTransition);
        }

        CurrentState = next;
        return C012TransitionResult.Accept(next);
    }

    private static List<C012Transition> BuildAllTransitions()
    {
        var all = new List<C012Transition>(HappyPath);
        foreach (C012State state in Enum.GetValues<C012State>())
        {
            if (state is C012State.Terminated or C012State.FailedClosed)
            {
                continue;
            }

            all.Add(new C012Transition(state, C012Trigger.Timeout, C012State.FailedClosed));
            all.Add(new C012Transition(state, C012Trigger.RootProcessDied, C012State.FailedClosed));
        }

        return all;
    }
}

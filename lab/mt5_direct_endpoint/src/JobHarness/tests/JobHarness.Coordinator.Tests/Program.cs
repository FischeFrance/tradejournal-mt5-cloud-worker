using TradeJournal.Lab.JobHarness.Coordinator;

var tests = new (string Name, Action Body)[]
{
    ("happy_path_c0_through_c2_reaches_terminated", HappyPathReachesTerminated),
    ("all_transitions_table_matches_expected_shape", AllTransitionsTableMatchesExpectedShape),
    ("all_illegal_transitions_are_rejected_without_mutating_state", AllIllegalTransitionsAreRejectedWithoutMutatingState),
    ("no_transition_ever_moves_backward_or_repeats_a_state", NoTransitionEverMovesBackwardOrRepeatsAState),
    ("timeout_from_several_states_fails_closed", TimeoutFromSeveralStatesFailsClosed),
    ("root_process_died_from_several_states_fails_closed", RootProcessDiedFromSeveralStatesFailsClosed),
    ("failed_closed_rejects_every_further_trigger", FailedClosedRejectsEveryFurtherTrigger),
    ("terminated_is_irreversible", TerminatedIsIrreversible),
    ("c1_is_accepted_only_after_c0_retained", C1IsAcceptedOnlyAfterC0Retained),
    ("c2_is_accepted_only_after_c1_retained", C2IsAcceptedOnlyAfterC1Retained),
    ("teardown_is_reachable_only_from_c2_teardown", TeardownIsReachableOnlyFromC2Teardown),
    ("protocol_violation_control_out_of_order_is_rejected_without_mutating_state", ProtocolViolationControlOutOfOrderIsRejectedWithoutMutatingState),
    ("duplicate_sequence_number_is_rejected_without_mutating_state", DuplicateSequenceNumberIsRejectedWithoutMutatingState),
    ("out_of_order_future_sequence_number_is_rejected_without_mutating_state", OutOfOrderFutureSequenceNumberIsRejectedWithoutMutatingState),
    ("session_mismatch_is_rejected_without_mutating_state", SessionMismatchIsRejectedWithoutMutatingState),
    ("apply_internal_refuses_client_originated_triggers", ApplyInternalRefusesClientOriginatedTriggers),
    ("unmapped_request_combination_is_rejected_without_mutating_state", UnmappedRequestCombinationIsRejectedWithoutMutatingState),
};

int failures = 0;
foreach ((string name, Action body) in tests)
{
    try
    {
        body();
        Console.WriteLine($"PASS {name}");
    }
    catch (Exception exception)
    {
        failures++;
        Console.Error.WriteLine($"FAIL {name}: {exception.GetType().Name}: {exception.Message}");
    }
}

return failures == 0 ? 0 : 1;

static void HappyPathReachesTerminated()
{
    var machine = new C012StateMachine();
    Assert(machine.CurrentState == C012State.NotStarted, "initial state");

    C012TransitionResult r0 = machine.Apply(C012Trigger.BeginC0);
    Assert(r0.Accepted && r0.ResultingState == C012State.C0JobCreating, "begin c0");

    C012TransitionResult r1 = machine.Apply(C012Trigger.C0JobCreated);
    Assert(r1.Accepted && r1.ResultingState == C012State.C0RootLaunching, "c0 job created");

    C012TransitionResult r2 = machine.Apply(C012Trigger.C0RootLaunched);
    Assert(r2.Accepted && r2.ResultingState == C012State.C0Retained, "c0 root launched / retained");

    C012TransitionResult r3 = machine.Apply(C012Trigger.BeginC1);
    Assert(r3.Accepted && r3.ResultingState == C012State.C1DiscoveryRunning, "begin c1");

    C012TransitionResult r4 = machine.Apply(C012Trigger.C1DiscoveryComplete);
    Assert(r4.Accepted && r4.ResultingState == C012State.C1Retained, "c1 discovery complete / retained");

    C012TransitionResult r5 = machine.Apply(C012Trigger.BeginC2);
    Assert(r5.Accepted && r5.ResultingState == C012State.C2Submitting, "begin c2");

    C012TransitionResult r6 = machine.Apply(C012Trigger.C2SubmitterAssigned);
    Assert(r6.Accepted && r6.ResultingState == C012State.C2LoginWindow, "c2 submitter assigned");

    C012TransitionResult r7 = machine.Apply(C012Trigger.C2LoginWindowComplete);
    Assert(r7.Accepted && r7.ResultingState == C012State.C2Teardown, "c2 login window complete");

    C012TransitionResult r8 = machine.Apply(C012Trigger.C2TeardownComplete);
    Assert(r8.Accepted && r8.ResultingState == C012State.Terminated, "c2 teardown complete / terminated");
}

static void AllTransitionsTableMatchesExpectedShape()
{
    var expectedHappyPath = new (C012State From, C012Trigger Trigger, C012State To)[]
    {
        (C012State.NotStarted, C012Trigger.BeginC0, C012State.C0JobCreating),
        (C012State.C0JobCreating, C012Trigger.C0JobCreated, C012State.C0RootLaunching),
        (C012State.C0RootLaunching, C012Trigger.C0RootLaunched, C012State.C0Retained),
        (C012State.C0Retained, C012Trigger.BeginC1, C012State.C1DiscoveryRunning),
        (C012State.C1DiscoveryRunning, C012Trigger.C1DiscoveryComplete, C012State.C1Retained),
        (C012State.C1Retained, C012Trigger.BeginC2, C012State.C2Submitting),
        (C012State.C2Submitting, C012Trigger.C2SubmitterAssigned, C012State.C2LoginWindow),
        (C012State.C2LoginWindow, C012Trigger.C2LoginWindowComplete, C012State.C2Teardown),
        (C012State.C2Teardown, C012Trigger.C2TeardownComplete, C012State.Terminated),
    };

    foreach ((C012State from, C012Trigger trigger, C012State to) in expectedHappyPath)
    {
        bool found = C012StateMachine.AllTransitions.Any(t => t.From == from && t.Trigger == trigger && t.To == to);
        Assert(found, $"expected happy-path transition missing: {from} + {trigger} -> {to}");
    }

    C012State[] nonTerminal = Enum.GetValues<C012State>()
        .Where(state => !C012StateMachine.TerminalStates.Contains(state))
        .ToArray();

    foreach (C012State state in nonTerminal)
    {
        bool hasTimeoutEdge = C012StateMachine.AllTransitions.Any(t =>
            t.From == state && t.Trigger == C012Trigger.Timeout && t.To == C012State.FailedClosed);
        bool hasRootDiedEdge = C012StateMachine.AllTransitions.Any(t =>
            t.From == state && t.Trigger == C012Trigger.RootProcessDied && t.To == C012State.FailedClosed);
        Assert(hasTimeoutEdge, $"missing Timeout edge from {state}");
        Assert(hasRootDiedEdge, $"missing RootProcessDied edge from {state}");
    }

    int expectedCount = expectedHappyPath.Length + (nonTerminal.Length * 2);
    Assert(C012StateMachine.AllTransitions.Count == expectedCount, "transition table size");
}

static void AllIllegalTransitionsAreRejectedWithoutMutatingState()
{
    foreach (C012State state in Enum.GetValues<C012State>())
    {
        bool isTerminal = C012StateMachine.TerminalStates.Contains(state);
        foreach (C012Trigger trigger in Enum.GetValues<C012Trigger>())
        {
            bool isLegal = C012StateMachine.AllTransitions.Any(t => t.From == state && t.Trigger == trigger);
            if (isLegal && !isTerminal)
            {
                continue;
            }

            C012StateMachine machine = MachineForcedInto(state);
            C012TransitionResult result = machine.Apply(trigger);
            Assert(!result.Accepted, $"{state} + {trigger} must be rejected");
            Assert(result.ResultingState == state, $"{state} + {trigger} must not mutate state");
            C012RejectionReason expectedReason =
                isTerminal ? C012RejectionReason.TerminalState : C012RejectionReason.IllegalTransition;
            Assert(result.Reason == expectedReason, $"{state} + {trigger} reason mismatch: {result.Reason}");
        }
    }
}

static void NoTransitionEverMovesBackwardOrRepeatsAState()
{
    var rank = new Dictionary<C012State, int>
    {
        [C012State.NotStarted] = 0,
        [C012State.C0JobCreating] = 1,
        [C012State.C0RootLaunching] = 2,
        [C012State.C0Retained] = 3,
        [C012State.C1DiscoveryRunning] = 4,
        [C012State.C1Retained] = 5,
        [C012State.C2Submitting] = 6,
        [C012State.C2LoginWindow] = 7,
        [C012State.C2Teardown] = 8,
        [C012State.Terminated] = 9,
    };

    foreach (C012Transition transition in C012StateMachine.AllTransitions)
    {
        if (transition.To == C012State.FailedClosed)
        {
            continue;
        }

        Assert(rank.ContainsKey(transition.From), $"unranked state {transition.From}");
        Assert(rank.ContainsKey(transition.To), $"unranked state {transition.To}");
        Assert(rank[transition.To] > rank[transition.From], $"backward or repeated transition: {transition}");
    }
}

static void TimeoutFromSeveralStatesFailsClosed()
{
    foreach (C012State state in new[]
             {
                 C012State.NotStarted, C012State.C1Retained, C012State.C2LoginWindow, C012State.C2Teardown,
             })
    {
        C012StateMachine machine = MachineForcedInto(state);
        C012TransitionResult result = machine.Apply(C012Trigger.Timeout);
        Assert(result.Accepted && result.ResultingState == C012State.FailedClosed, $"timeout from {state} must fail closed");
    }
}

static void RootProcessDiedFromSeveralStatesFailsClosed()
{
    foreach (C012State state in new[]
             {
                 C012State.C0RootLaunching, C012State.C0Retained, C012State.C2Submitting,
             })
    {
        C012StateMachine machine = MachineForcedInto(state);
        C012TransitionResult result = machine.Apply(C012Trigger.RootProcessDied);
        Assert(result.Accepted && result.ResultingState == C012State.FailedClosed, $"root died from {state} must fail closed");
    }
}

static void FailedClosedRejectsEveryFurtherTrigger()
{
    var machine = new C012StateMachine();
    _ = machine.Apply(C012Trigger.Timeout);
    Assert(machine.CurrentState == C012State.FailedClosed, "setup: expected FailedClosed");

    foreach (C012Trigger trigger in Enum.GetValues<C012Trigger>())
    {
        C012TransitionResult result = machine.Apply(trigger);
        Assert(!result.Accepted, $"FailedClosed must reject {trigger}");
        Assert(result.ResultingState == C012State.FailedClosed, "FailedClosed must not change");
        Assert(result.Reason == C012RejectionReason.TerminalState, "FailedClosed rejection reason");
    }
}

static void TerminatedIsIrreversible()
{
    C012StateMachine machine = MachineForcedInto(C012State.Terminated);

    foreach (C012Trigger trigger in Enum.GetValues<C012Trigger>())
    {
        C012TransitionResult result = machine.Apply(trigger);
        Assert(!result.Accepted, $"Terminated must reject {trigger}");
        Assert(result.ResultingState == C012State.Terminated, "Terminated must not change");
        Assert(result.Reason == C012RejectionReason.TerminalState, "Terminated rejection reason");
    }
}

static void C1IsAcceptedOnlyAfterC0Retained()
{
    foreach (C012State state in Enum.GetValues<C012State>())
    {
        if (state == C012State.C0Retained)
        {
            continue;
        }

        C012StateMachine machine = MachineForcedInto(state);
        C012TransitionResult result = machine.Apply(C012Trigger.BeginC1);
        Assert(!result.Accepted, $"BeginC1 from {state} must be rejected");
    }

    C012StateMachine ready = MachineForcedInto(C012State.C0Retained);
    Assert(ready.Apply(C012Trigger.BeginC1).Accepted, "BeginC1 from C0Retained must be accepted");
}

static void C2IsAcceptedOnlyAfterC1Retained()
{
    foreach (C012State state in Enum.GetValues<C012State>())
    {
        if (state == C012State.C1Retained)
        {
            continue;
        }

        C012StateMachine machine = MachineForcedInto(state);
        C012TransitionResult result = machine.Apply(C012Trigger.BeginC2);
        Assert(!result.Accepted, $"BeginC2 from {state} must be rejected");
    }

    C012StateMachine ready = MachineForcedInto(C012State.C1Retained);
    Assert(ready.Apply(C012Trigger.BeginC2).Accepted, "BeginC2 from C1Retained must be accepted");
}

static void TeardownIsReachableOnlyFromC2Teardown()
{
    List<C012Transition> toTerminated = C012StateMachine.AllTransitions
        .Where(t => t.To == C012State.Terminated)
        .ToList();
    Assert(toTerminated.Count == 1, "exactly one transition may reach Terminated");
    Assert(
        toTerminated[0] is { From: C012State.C2Teardown, Trigger: C012Trigger.C2TeardownComplete },
        "the only teardown edge must originate in C2Teardown");

    foreach (C012State state in new[]
             {
                 C012State.C0Retained, C012State.C1Retained, C012State.C1DiscoveryRunning,
             })
    {
        C012StateMachine machine = MachineForcedInto(state);
        C012TransitionResult result = machine.Apply(C012Trigger.C2TeardownComplete);
        Assert(!result.Accepted, $"C2TeardownComplete from {state} must be rejected");
    }
}

static void ProtocolViolationControlOutOfOrderIsRejectedWithoutMutatingState()
{
    Guid sessionId = Guid.NewGuid();
    var sequencer = new C012RequestSequencer(sessionId);

    C012TransitionResult result = sequencer.Apply(
        new C012RequestEnvelope(sessionId, 1, C012Control.C2, C012RequestType.SubmitConfig));
    Assert(!result.Accepted, "C2 before C0/C1 must be rejected");
    Assert(result.Reason == C012RejectionReason.IllegalTransition, "protocol violation reason");
    Assert(sequencer.CurrentState == C012State.NotStarted, "state must not mutate");

    C012TransitionResult recovered = sequencer.Apply(
        new C012RequestEnvelope(sessionId, 1, C012Control.C0, C012RequestType.Start));
    Assert(recovered.Accepted && recovered.ResultingState == C012State.C0JobCreating, "recovery after violation");
}

static void DuplicateSequenceNumberIsRejectedWithoutMutatingState()
{
    Guid sessionId = Guid.NewGuid();
    var sequencer = new C012RequestSequencer(sessionId);

    C012TransitionResult first = sequencer.Apply(
        new C012RequestEnvelope(sessionId, 1, C012Control.C0, C012RequestType.Start));
    Assert(first.Accepted, "first request must be accepted");

    C012TransitionResult duplicate = sequencer.Apply(
        new C012RequestEnvelope(sessionId, 1, C012Control.C0, C012RequestType.Start));
    Assert(!duplicate.Accepted, "duplicate sequence number must be rejected");
    Assert(duplicate.Reason == C012RejectionReason.SequenceOutOfOrder, "duplicate rejection reason");
    Assert(duplicate.ResultingState == C012State.C0JobCreating, "state must not mutate on duplicate");
}

static void OutOfOrderFutureSequenceNumberIsRejectedWithoutMutatingState()
{
    Guid sessionId = Guid.NewGuid();
    var sequencer = new C012RequestSequencer(sessionId);

    C012TransitionResult result = sequencer.Apply(
        new C012RequestEnvelope(sessionId, 3, C012Control.C0, C012RequestType.Start));
    Assert(!result.Accepted, "future sequence number must be rejected");
    Assert(result.Reason == C012RejectionReason.SequenceOutOfOrder, "future sequence rejection reason");
    Assert(result.ResultingState == C012State.NotStarted, "state must not mutate");
}

static void SessionMismatchIsRejectedWithoutMutatingState()
{
    Guid sessionId = Guid.NewGuid();
    Guid otherSessionId = Guid.NewGuid();
    var sequencer = new C012RequestSequencer(sessionId);

    C012TransitionResult result = sequencer.Apply(
        new C012RequestEnvelope(otherSessionId, 1, C012Control.C0, C012RequestType.Start));
    Assert(!result.Accepted, "session mismatch must be rejected");
    Assert(result.Reason == C012RejectionReason.SessionMismatch, "session mismatch reason");
    Assert(result.ResultingState == C012State.NotStarted, "state must not mutate");

    C012TransitionResult recovered = sequencer.Apply(
        new C012RequestEnvelope(sessionId, 1, C012Control.C0, C012RequestType.Start));
    Assert(recovered.Accepted, "correct session must still succeed at seq=1");
}

static void ApplyInternalRefusesClientOriginatedTriggers()
{
    var sequencer = new C012RequestSequencer(Guid.NewGuid());
    foreach (C012Trigger trigger in new[] { C012Trigger.BeginC0, C012Trigger.BeginC1, C012Trigger.BeginC2 })
    {
        bool threw = false;
        try
        {
            sequencer.ApplyInternal(trigger);
        }
        catch (ArgumentException)
        {
            threw = true;
        }

        Assert(threw, $"ApplyInternal must refuse client-originated trigger {trigger}");
    }
}

static void UnmappedRequestCombinationIsRejectedWithoutMutatingState()
{
    Guid sessionId = Guid.NewGuid();
    var sequencer = new C012RequestSequencer(sessionId);

    C012TransitionResult result = sequencer.Apply(
        new C012RequestEnvelope(sessionId, 1, C012Control.C0, C012RequestType.SubmitConfig));
    Assert(!result.Accepted, "an invalid control/request-type combination must be rejected");
    Assert(result.Reason == C012RejectionReason.InvalidRequest, "invalid request reason");
    Assert(result.ResultingState == C012State.NotStarted, "state must not mutate");
}

static C012StateMachine MachineForcedInto(C012State target)
{
    var machine = new C012StateMachine();
    if (target == C012State.FailedClosed)
    {
        _ = machine.Apply(C012Trigger.Timeout);
        Assert(machine.CurrentState == C012State.FailedClosed, "setup: did not reach FailedClosed");
        return machine;
    }

    foreach (C012Trigger trigger in HappyPathTriggersUpTo(target))
    {
        C012TransitionResult result = machine.Apply(trigger);
        Assert(result.Accepted, $"setup: failed driving to {target} via {trigger}");
    }

    Assert(machine.CurrentState == target, $"setup: did not reach {target}");
    return machine;
}

static C012Trigger[] HappyPathTriggersUpTo(C012State target)
{
    var orderedStates = new[]
    {
        C012State.NotStarted,
        C012State.C0JobCreating,
        C012State.C0RootLaunching,
        C012State.C0Retained,
        C012State.C1DiscoveryRunning,
        C012State.C1Retained,
        C012State.C2Submitting,
        C012State.C2LoginWindow,
        C012State.C2Teardown,
        C012State.Terminated,
    };
    var orderedTriggers = new[]
    {
        C012Trigger.BeginC0,
        C012Trigger.C0JobCreated,
        C012Trigger.C0RootLaunched,
        C012Trigger.BeginC1,
        C012Trigger.C1DiscoveryComplete,
        C012Trigger.BeginC2,
        C012Trigger.C2SubmitterAssigned,
        C012Trigger.C2LoginWindowComplete,
        C012Trigger.C2TeardownComplete,
    };

    int targetIndex = Array.IndexOf(orderedStates, target);
    Assert(targetIndex >= 0, $"{target} is not on the happy path");
    return orderedTriggers.Take(targetIndex).ToArray();
}

static void Assert(bool condition, string message)
{
    if (!condition)
    {
        throw new InvalidOperationException(message);
    }
}

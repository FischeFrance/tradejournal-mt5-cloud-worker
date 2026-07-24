using System.Text.Json;
using System.Threading.Channels;
using TradeJournal.Lab.JobHarness.Coordinator;

var tests = new (string Name, Action Body)[]
{
    ("happy_path_c0_through_c2_reaches_terminated", HappyPathReachesTerminated),
    ("all_transitions_table_matches_expected_shape", AllTransitionsTableMatchesExpectedShape),
    ("all_illegal_transitions_are_rejected_without_mutating_state", AllIllegalTransitionsAreRejectedWithoutMutatingState),
    ("no_transition_ever_moves_backward_or_repeats_a_state", NoTransitionEverMovesBackwardOrRepeatsAState),
    ("timeout_from_several_states_fails_closed", TimeoutFromSeveralStatesFailsClosed),
    ("root_process_died_from_several_states_fails_closed", RootProcessDiedFromSeveralStatesFailsClosed),
    ("operation_failed_from_several_states_fails_closed", OperationFailedFromSeveralStatesFailsClosed),
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

    // B3: framing
    ("wire_frame_round_trip_preserves_bytes_exactly", WireFrameRoundTripPreservesBytesExactly),
    ("wire_frame_write_rejects_zero_length_payload", WireFrameWriteRejectsZeroLengthPayload),
    ("wire_frame_write_rejects_oversized_payload", WireFrameWriteRejectsOversizedPayload),
    ("wire_frame_read_returns_null_on_clean_eof_before_any_byte", WireFrameReadReturnsNullOnCleanEofBeforeAnyByte),
    ("wire_frame_read_throws_on_truncated_header", WireFrameReadThrowsOnTruncatedHeader),
    ("wire_frame_read_throws_on_truncated_payload", WireFrameReadThrowsOnTruncatedPayload),
    ("wire_frame_read_rejects_declared_length_over_max", WireFrameReadRejectsDeclaredLengthOverMax),
    ("wire_frame_read_reassembles_partial_byte_by_byte_reads", WireFrameReadReassemblesPartialByteByByteReads),

    // B3: canonical payload + authentication
    ("canonical_payload_is_deterministic_for_identical_fields", CanonicalPayloadIsDeterministicForIdenticalFields),
    ("authenticator_sign_then_verify_request_succeeds", AuthenticatorSignThenVerifyRequestSucceeds),
    ("authenticator_sign_then_verify_response_succeeds", AuthenticatorSignThenVerifyResponseSucceeds),
    ("authenticator_verify_fails_with_different_secret", AuthenticatorVerifyFailsWithDifferentSecret),
    ("authenticator_verify_fails_when_signed_fields_are_tampered", AuthenticatorVerifyFailsWhenSignedFieldsAreTampered),
    ("authenticator_verify_fails_when_hmac_bit_is_flipped", AuthenticatorVerifyFailsWhenHmacBitIsFlipped),

    // B3: wire validation
    ("wire_validation_rejects_wrong_schema_version", WireValidationRejectsWrongSchemaVersion),
    ("wire_validation_rejects_malformed_hmac_hex", WireValidationRejectsMalformedHmacHex),

    // B3: session secret
    ("session_secret_generate_produces_distinct_values", SessionSecretGenerateProducesDistinctValues),
    ("session_secret_save_then_load_round_trips_exact_bytes", SessionSecretSaveThenLoadRoundTripsExactBytes),
    ("session_secret_save_refuses_to_overwrite_existing_file", SessionSecretSaveRefusesToOverwriteExistingFile),
    ("session_secret_save_applies_windows_only_acl", SessionSecretSaveAppliesWindowsOnlyAcl),

    // B3: server channel
    ("server_channel_accepts_wellformed_signed_c0_start", ServerChannelAcceptsWellformedSignedC0Start),
    ("server_channel_rejects_wrong_hmac_without_mutating_sequencer_or_responding", ServerChannelRejectsWrongHmacWithoutMutatingSequencerOrResponding),
    ("server_channel_rejects_malformed_frame_without_mutating_sequencer", ServerChannelRejectsMalformedFrameWithoutMutatingSequencer),
    ("server_channel_delegates_sequencer_rejection_and_signs_response", ServerChannelDelegatesSequencerRejectionAndSignsResponse),
    ("server_channel_replaying_identical_rejected_request_is_rejected_identically", ServerChannelReplayingIdenticalRejectedRequestIsRejectedIdentically),

    // B3: client/server round trip
    ("client_server_round_trip_happy_path_over_duplex_streams", ClientServerRoundTripHappyPathOverDuplexStreams),
    ("client_rejects_response_with_wrong_hmac", ClientRejectsResponseWithWrongHmac),
    ("client_keeps_same_sequence_slot_after_server_rejection", ClientKeepsSameSequenceSlotAfterServerRejection),

    // B3: duplex stream EOF/closure
    ("channel_stream_read_returns_zero_after_writer_completes", ChannelStreamReadReturnsZeroAfterWriterCompletes),
    ("duplex_pair_dispose_completes_both_channels_without_hanging", DuplexPairDisposeCompletesBothChannelsWithoutHanging),

    // B4.1: orchestrating processor
    ("orchestrating_processor_c0_success_calls_launcher_in_order_and_reaches_c0_retained", OrchestratingProcessorC0SuccessCallsLauncherInOrderAndReachesC0Retained),
    ("orchestrating_processor_c0_fails_when_create_job_throws", OrchestratingProcessorC0FailsWhenCreateJobThrows),
    ("orchestrating_processor_c0_fails_when_launch_root_throws", OrchestratingProcessorC0FailsWhenLaunchRootThrows),
    ("orchestrating_processor_c0_fails_when_assign_root_throws", OrchestratingProcessorC0FailsWhenAssignRootThrows),
    ("orchestrating_processor_c0_fails_when_resume_root_throws", OrchestratingProcessorC0FailsWhenResumeRootThrows),
    ("orchestrating_processor_c1_success_reaches_c1_retained", OrchestratingProcessorC1SuccessReachesC1Retained),
    ("orchestrating_processor_c1_fails_root_died_when_verify_root_alive_returns_false", OrchestratingProcessorC1FailsRootDiedWhenVerifyRootAliveReturnsFalse),
    ("orchestrating_processor_c1_fails_operation_when_verify_root_alive_throws", OrchestratingProcessorC1FailsOperationWhenVerifyRootAliveThrows),
    ("orchestrating_processor_c2_fails_when_launch_submitter_throws", OrchestratingProcessorC2FailsWhenLaunchSubmitterThrows),
    ("orchestrating_processor_c2_fails_when_assign_submitter_throws", OrchestratingProcessorC2FailsWhenAssignSubmitterThrows),
    ("orchestrating_processor_c2_fails_when_verify_same_job_returns_false", OrchestratingProcessorC2FailsWhenVerifySameJobReturnsFalse),
    ("orchestrating_processor_c2_fails_when_verify_same_job_throws", OrchestratingProcessorC2FailsWhenVerifySameJobThrows),
    ("orchestrating_processor_c2_success_reaches_terminated_and_tears_down_exactly_once", OrchestratingProcessorC2SuccessReachesTerminatedAndTearsDownExactlyOnce),
    ("orchestrating_processor_c2_fails_when_teardown_throws_and_does_not_double_teardown", OrchestratingProcessorC2FailsWhenTeardownThrowsAndDoesNotDoubleTeardown),
    ("orchestrating_processor_never_calls_launcher_once_failed_closed", OrchestratingProcessorNeverCallsLauncherOnceFailedClosed),
    ("orchestrating_processor_rejects_illegal_request_without_touching_launcher", OrchestratingProcessorRejectsIllegalRequestWithoutTouchingLauncher),
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
        bool hasOperationFailedEdge = C012StateMachine.AllTransitions.Any(t =>
            t.From == state && t.Trigger == C012Trigger.OperationFailed && t.To == C012State.FailedClosed);
        Assert(hasTimeoutEdge, $"missing Timeout edge from {state}");
        Assert(hasRootDiedEdge, $"missing RootProcessDied edge from {state}");
        Assert(hasOperationFailedEdge, $"missing OperationFailed edge from {state}");
    }

    int expectedCount = expectedHappyPath.Length + (nonTerminal.Length * 3);
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

static void OperationFailedFromSeveralStatesFailsClosed()
{
    foreach (C012State state in new[]
             {
                 C012State.C0JobCreating, C012State.C1DiscoveryRunning, C012State.C2LoginWindow,
             })
    {
        C012StateMachine machine = MachineForcedInto(state);
        C012TransitionResult result = machine.Apply(C012Trigger.OperationFailed);
        Assert(result.Accepted && result.ResultingState == C012State.FailedClosed, $"operation failed from {state} must fail closed");
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

// ---- B3: framing ----

static void WireFrameRoundTripPreservesBytesExactly()
{
    byte[] payload = "hello-frame"u8.ToArray();
    var stream = new MemoryStream();
    C012FrameCodec.WriteFrameAsync(stream, payload, CancellationToken.None).GetAwaiter().GetResult();
    stream.Position = 0;
    byte[]? read = C012FrameCodec.ReadFrameAsync(stream, CancellationToken.None).GetAwaiter().GetResult();
    Assert(read is not null, "frame must round-trip");
    Assert(read!.AsSpan().SequenceEqual(payload), "frame bytes must match exactly");
}

static void WireFrameWriteRejectsZeroLengthPayload()
{
    var stream = new MemoryStream();
    bool threw = false;
    try
    {
        C012FrameCodec.WriteFrameAsync(stream, ReadOnlyMemory<byte>.Empty, CancellationToken.None).GetAwaiter().GetResult();
    }
    catch (ArgumentOutOfRangeException)
    {
        threw = true;
    }

    Assert(threw, "zero-length payload must be rejected");
}

static void WireFrameWriteRejectsOversizedPayload()
{
    var stream = new MemoryStream();
    byte[] oversized = new byte[C012FrameCodec.MaxFrameLength + 1];
    bool threw = false;
    try
    {
        C012FrameCodec.WriteFrameAsync(stream, oversized, CancellationToken.None).GetAwaiter().GetResult();
    }
    catch (ArgumentOutOfRangeException)
    {
        threw = true;
    }

    Assert(threw, "oversized payload must be rejected");
}

static void WireFrameReadReturnsNullOnCleanEofBeforeAnyByte()
{
    var stream = new MemoryStream();
    byte[]? read = C012FrameCodec.ReadFrameAsync(stream, CancellationToken.None).GetAwaiter().GetResult();
    Assert(read is null, "clean EOF before any byte must return null, not throw");
}

static void WireFrameReadThrowsOnTruncatedHeader()
{
    var stream = new MemoryStream([0x00, 0x00]);
    bool threw = false;
    try
    {
        C012FrameCodec.ReadFrameAsync(stream, CancellationToken.None).GetAwaiter().GetResult();
    }
    catch (C012FramingException)
    {
        threw = true;
    }

    Assert(threw, "a truncated header must throw, not return null");
}

static void WireFrameReadThrowsOnTruncatedPayload()
{
    var stream = new MemoryStream();
    C012FrameCodec.WriteFrameAsync(stream, new byte[10], CancellationToken.None).GetAwaiter().GetResult();
    stream.SetLength(stream.Length - 3);
    stream.Position = 0;
    bool threw = false;
    try
    {
        C012FrameCodec.ReadFrameAsync(stream, CancellationToken.None).GetAwaiter().GetResult();
    }
    catch (C012FramingException)
    {
        threw = true;
    }

    Assert(threw, "a truncated payload must throw");
}

static void WireFrameReadRejectsDeclaredLengthOverMax()
{
    var header = new byte[4];
    System.Buffers.Binary.BinaryPrimitives.WriteUInt32BigEndian(header, C012FrameCodec.MaxFrameLength + 1);
    var stream = new MemoryStream(header);
    bool threw = false;
    try
    {
        C012FrameCodec.ReadFrameAsync(stream, CancellationToken.None).GetAwaiter().GetResult();
    }
    catch (C012FramingException)
    {
        threw = true;
    }

    Assert(threw, "a declared length over the maximum must be rejected");
}

static void WireFrameReadReassemblesPartialByteByByteReads()
{
    byte[] payload = "reassembled-across-many-tiny-reads"u8.ToArray();
    var framed = new MemoryStream();
    C012FrameCodec.WriteFrameAsync(framed, payload, CancellationToken.None).GetAwaiter().GetResult();

    using var slowStream = new OneByteAtATimeStream(framed.ToArray());
    byte[]? read = C012FrameCodec.ReadFrameAsync(slowStream, CancellationToken.None).GetAwaiter().GetResult();
    Assert(read is not null, "frame must still be read when delivered one byte at a time");
    Assert(read!.AsSpan().SequenceEqual(payload), "reassembled bytes must match exactly");
}

// ---- B3: canonical payload + authentication ----

static void CanonicalPayloadIsDeterministicForIdenticalFields()
{
    Guid sessionId = Guid.NewGuid();
    byte[] first = C012CanonicalPayload.ForRequest(1, sessionId, 7, C012Control.C1, C012RequestType.Query);
    byte[] second = C012CanonicalPayload.ForRequest(1, sessionId, 7, C012Control.C1, C012RequestType.Query);
    Assert(first.AsSpan().SequenceEqual(second), "identical fields must produce byte-identical canonical payloads");
}

static void AuthenticatorSignThenVerifyRequestSucceeds()
{
    byte[] secret = System.Security.Cryptography.RandomNumberGenerator.GetBytes(32);
    Guid sessionId = Guid.NewGuid();
    string hmac = C012MessageAuthenticator.SignRequest(secret, 1, sessionId, 1, C012Control.C0, C012RequestType.Start);
    var request = new C012WireRequest(1, sessionId, 1, C012Control.C0, C012RequestType.Start, hmac);
    Assert(C012MessageAuthenticator.VerifyRequest(secret, request), "signed request must verify with the same secret");
}

static void AuthenticatorSignThenVerifyResponseSucceeds()
{
    byte[] secret = System.Security.Cryptography.RandomNumberGenerator.GetBytes(32);
    Guid sessionId = Guid.NewGuid();
    string hmac = C012MessageAuthenticator.SignResponse(
        secret, 1, sessionId, 1, true, C012State.C0JobCreating, C012RejectionReason.None);
    var response = new C012WireResponse(1, sessionId, 1, true, C012State.C0JobCreating, C012RejectionReason.None, hmac);
    Assert(C012MessageAuthenticator.VerifyResponse(secret, response), "signed response must verify with the same secret");
}

static void AuthenticatorVerifyFailsWithDifferentSecret()
{
    byte[] secretA = System.Security.Cryptography.RandomNumberGenerator.GetBytes(32);
    byte[] secretB = System.Security.Cryptography.RandomNumberGenerator.GetBytes(32);
    Guid sessionId = Guid.NewGuid();
    string hmac = C012MessageAuthenticator.SignRequest(secretA, 1, sessionId, 1, C012Control.C0, C012RequestType.Start);
    var request = new C012WireRequest(1, sessionId, 1, C012Control.C0, C012RequestType.Start, hmac);
    Assert(!C012MessageAuthenticator.VerifyRequest(secretB, request), "verification with a different secret must fail");
}

static void AuthenticatorVerifyFailsWhenSignedFieldsAreTampered()
{
    byte[] secret = System.Security.Cryptography.RandomNumberGenerator.GetBytes(32);
    Guid sessionId = Guid.NewGuid();
    string hmac = C012MessageAuthenticator.SignRequest(secret, 1, sessionId, 1, C012Control.C0, C012RequestType.Start);

    var tamperedSequence = new C012WireRequest(1, sessionId, 2, C012Control.C0, C012RequestType.Start, hmac);
    Assert(!C012MessageAuthenticator.VerifyRequest(secret, tamperedSequence), "tampered sequence number must fail verification");

    var tamperedSession = new C012WireRequest(1, Guid.NewGuid(), 1, C012Control.C0, C012RequestType.Start, hmac);
    Assert(!C012MessageAuthenticator.VerifyRequest(secret, tamperedSession), "tampered session id must fail verification");

    var tamperedControl = new C012WireRequest(1, sessionId, 1, C012Control.C1, C012RequestType.Start, hmac);
    Assert(!C012MessageAuthenticator.VerifyRequest(secret, tamperedControl), "tampered control must fail verification");
}

static void AuthenticatorVerifyFailsWhenHmacBitIsFlipped()
{
    byte[] secret = System.Security.Cryptography.RandomNumberGenerator.GetBytes(32);
    Guid sessionId = Guid.NewGuid();
    string hmac = C012MessageAuthenticator.SignRequest(secret, 1, sessionId, 1, C012Control.C0, C012RequestType.Start);
    char flipped = hmac[0] == '0' ? '1' : '0';
    string tamperedHmac = flipped + hmac[1..];
    var request = new C012WireRequest(1, sessionId, 1, C012Control.C0, C012RequestType.Start, tamperedHmac);
    Assert(!C012MessageAuthenticator.VerifyRequest(secret, request), "a flipped HMAC character must fail verification");
}

// ---- B3: wire validation ----

static void WireValidationRejectsWrongSchemaVersion()
{
    var request = new C012WireRequest(
        99, Guid.NewGuid(), 1, C012Control.C0, C012RequestType.Start, new string('a', 64));
    Assert(!C012WireValidation.IsValid(request), "an unsupported schema version must be rejected");
}

static void WireValidationRejectsMalformedHmacHex()
{
    var tooShort = new C012WireRequest(1, Guid.NewGuid(), 1, C012Control.C0, C012RequestType.Start, "abc");
    Assert(!C012WireValidation.IsValid(tooShort), "a too-short hmac must be rejected");

    var upperCase = new C012WireRequest(1, Guid.NewGuid(), 1, C012Control.C0, C012RequestType.Start, new string('A', 64));
    Assert(!C012WireValidation.IsValid(upperCase), "an upper-case hmac must be rejected");
}

// ---- B3: session secret ----

static void SessionSecretGenerateProducesDistinctValues()
{
    using C012SessionSecret first = C012SessionSecret.Generate();
    using C012SessionSecret second = C012SessionSecret.Generate();
    Assert(!first.Value.SequenceEqual(second.Value), "two generated secrets must not be equal");
    Assert(first.Value.Length == C012SessionSecret.LengthBytes, "secret must be 256 bits");
}

static void SessionSecretSaveThenLoadRoundTripsExactBytes()
{
    string directory = CreateTemporaryDirectory();
    try
    {
        string path = Path.Combine(directory, "session.secret");
        byte[] original;
        using (C012SessionSecret secret = C012SessionSecret.Generate())
        {
            original = secret.Value.ToArray();
            secret.Save(path);
        }

        using C012SessionSecret loaded = C012SessionSecret.Load(path);
        Assert(loaded.Value.SequenceEqual(original), "loaded secret must match the saved bytes exactly");
    }
    finally
    {
        Directory.Delete(directory, recursive: true);
    }
}

static void SessionSecretSaveRefusesToOverwriteExistingFile()
{
    string directory = CreateTemporaryDirectory();
    try
    {
        string path = Path.Combine(directory, "session.secret");
        using C012SessionSecret first = C012SessionSecret.Generate();
        first.Save(path);

        using C012SessionSecret second = C012SessionSecret.Generate();
        bool threw = false;
        try
        {
            second.Save(path);
        }
        catch (IOException)
        {
            threw = true;
        }

        Assert(threw, "saving to an existing path must be refused");
    }
    finally
    {
        Directory.Delete(directory, recursive: true);
    }
}

static void SessionSecretSaveAppliesWindowsOnlyAcl()
{
    if (!OperatingSystem.IsWindows())
    {
        return;
    }

    string directory = CreateTemporaryDirectory();
    try
    {
        string path = Path.Combine(directory, "session.secret");
        using (C012SessionSecret secret = C012SessionSecret.Generate())
        {
            secret.Save(path);
        }

        var fileInfo = new FileInfo(path);
        System.Security.AccessControl.FileSecurity security = fileInfo.GetAccessControl();
        Assert(security.AreAccessRulesProtected, "the secret file must not inherit ACEs from its parent directory");

        var rules = security.GetAccessRules(true, true, typeof(System.Security.Principal.SecurityIdentifier));
        System.Security.Principal.SecurityIdentifier currentUser =
            System.Security.Principal.WindowsIdentity.GetCurrent().User
            ?? throw new InvalidOperationException("current user SID unavailable");

        Assert(rules.Count == 1, "the secret file must carry exactly one access rule");
        var onlyRule = (System.Security.AccessControl.FileSystemAccessRule)rules[0]!;
        var ruleSid = (System.Security.Principal.SecurityIdentifier)onlyRule.IdentityReference;
        Assert(ruleSid.Value == currentUser.Value, "the single access rule must belong to the current user");
    }
    finally
    {
        Directory.Delete(directory, recursive: true);
    }
}

// ---- B3: server channel ----

static void ServerChannelAcceptsWellformedSignedC0Start()
{
    using C012SessionSecret secret = C012SessionSecret.Generate();
    Guid sessionId = Guid.NewGuid();
    var sequencer = new C012RequestSequencer(sessionId);

    ExchangeSingleRequest(secret, sequencer, secret, sessionId, 1, C012Control.C0, C012RequestType.Start,
        out C012ChannelOutcome outcome, out C012WireResponse? response);

    Assert(outcome == C012ChannelOutcome.RequestProcessed, "well-formed request must be processed");
    Assert(response is not null && response.Accepted, "C0 start from NotStarted must be accepted");
    Assert(response!.ResultingState == C012State.C0JobCreating, "resulting state must be C0JobCreating");
    Assert(C012MessageAuthenticator.VerifyResponse(secret.Value, response!), "response must be validly signed");
    Assert(sequencer.CurrentState == C012State.C0JobCreating, "sequencer must reflect the accepted transition");
}

static void ServerChannelRejectsWrongHmacWithoutMutatingSequencerOrResponding()
{
    using C012SessionSecret serverSecret = C012SessionSecret.Generate();
    using C012SessionSecret wrongSecret = C012SessionSecret.Generate();
    Guid sessionId = Guid.NewGuid();
    var sequencer = new C012RequestSequencer(sessionId);

    ExchangeSingleRequest(serverSecret, sequencer, wrongSecret, sessionId, 1, C012Control.C0, C012RequestType.Start,
        out C012ChannelOutcome outcome, out C012WireResponse? response, expectNoResponse: true);

    Assert(outcome == C012ChannelOutcome.AuthenticationFailed, "wrong HMAC must be reported as authentication failure");
    Assert(response is null, "no response frame may be sent for an unauthenticated request");
    Assert(sequencer.CurrentState == C012State.NotStarted, "sequencer state must not move on authentication failure");
}

static void ServerChannelRejectsMalformedFrameWithoutMutatingSequencer()
{
    using C012SessionSecret secret = C012SessionSecret.Generate();
    Guid sessionId = Guid.NewGuid();
    var sequencer = new C012RequestSequencer(sessionId);
    var stream = new MemoryStream();
    C012FrameCodec.WriteFrameAsync(stream, "not valid json"u8.ToArray(), CancellationToken.None).GetAwaiter().GetResult();
    stream.Position = 0;

    var server = new C012ServerChannel(stream, secret, sequencer);
    C012ChannelOutcome outcome = server.ProcessNextRequestAsync(CancellationToken.None).GetAwaiter().GetResult();

    Assert(outcome == C012ChannelOutcome.Malformed, "invalid JSON must be reported as malformed");
    Assert(sequencer.CurrentState == C012State.NotStarted, "sequencer state must not move on a malformed frame");
}

static void ServerChannelDelegatesSequencerRejectionAndSignsResponse()
{
    using C012SessionSecret secret = C012SessionSecret.Generate();
    Guid sessionId = Guid.NewGuid();
    var sequencer = new C012RequestSequencer(sessionId);

    // C2 before C0/C1: a protocol violation the underlying sequencer already rejects.
    ExchangeSingleRequest(secret, sequencer, secret, sessionId, 1, C012Control.C2, C012RequestType.SubmitConfig,
        out C012ChannelOutcome outcome, out C012WireResponse? response);

    Assert(outcome == C012ChannelOutcome.RequestProcessed, "an authenticated but FSM-rejected request still gets a response");
    Assert(response is not null && !response.Accepted, "premature C2 must be rejected by the FSM");
    Assert(response!.Reason == C012RejectionReason.IllegalTransition, "rejection reason must surface the FSM's reason");
    Assert(C012MessageAuthenticator.VerifyResponse(secret.Value, response!), "rejection response must still be signed");
    Assert(sequencer.CurrentState == C012State.NotStarted, "a rejected request must not advance sequencer state");
}

static void ServerChannelReplayingIdenticalRejectedRequestIsRejectedIdentically()
{
    using C012SessionSecret secret = C012SessionSecret.Generate();
    Guid sessionId = Guid.NewGuid();
    var sequencer = new C012RequestSequencer(sessionId);

    ExchangeSingleRequest(secret, sequencer, secret, sessionId, 1, C012Control.C2, C012RequestType.SubmitConfig,
        out C012ChannelOutcome firstOutcome, out C012WireResponse? firstResponse);
    ExchangeSingleRequest(secret, sequencer, secret, sessionId, 1, C012Control.C2, C012RequestType.SubmitConfig,
        out C012ChannelOutcome secondOutcome, out C012WireResponse? secondResponse);

    Assert(firstOutcome == C012ChannelOutcome.RequestProcessed && secondOutcome == C012ChannelOutcome.RequestProcessed,
        "resending an identical rejected request must still be processed, not blocked");
    Assert(!firstResponse!.Accepted && !secondResponse!.Accepted, "both attempts must be rejected");
    Assert(firstResponse!.Reason == secondResponse!.Reason, "the resend must be rejected for the identical reason");
    Assert(sequencer.CurrentState == C012State.NotStarted, "state must never move as a result of a rejected replay");
}

// ---- B3: client/server round trip ----

// No Task.WhenAll here: ChannelWriter.WriteAsync on an unbounded channel always completes
// synchronously (there is no capacity to wait for), so client.SendAsync(...) runs its write
// inline on this thread and only genuinely suspends once it reaches the response read.
// Calling the server synchronously right after therefore always finds the request already
// sitting in the channel -- and once the server writes its response the same way, the
// client task (awaited last) always finds it waiting too. This removes any dependency on
// task-scheduler interleaving, which is what made the two earlier attempts at this test
// (anonymous pipes, then Task.WhenAll over Channel-backed streams) hang in CI.
static void ClientServerRoundTripHappyPathOverDuplexStreams()
{
    using C012SessionSecret secret = C012SessionSecret.Generate();
    Guid sessionId = Guid.NewGuid();
    var sequencer = new C012RequestSequencer(sessionId);

    using DuplexPair pair = DuplexPair.Create();
    var server = new C012ServerChannel(pair.ServerStream, secret, sequencer);
    var client = new C012ClientChannel(pair.ClientStream, secret, sessionId);

    CancellationToken timeout = ShortTestTimeout();
    Task<C012TransitionResult> clientTask = client.SendAsync(C012Control.C0, C012RequestType.Start, timeout);
    C012ChannelOutcome serverOutcome = server.ProcessNextRequestAsync(timeout).GetAwaiter().GetResult();
    C012TransitionResult clientResult = clientTask.GetAwaiter().GetResult();

    Assert(serverOutcome == C012ChannelOutcome.RequestProcessed, "server must process the request");
    Assert(clientResult.Accepted, "client must observe acceptance");
    Assert(clientResult.ResultingState == C012State.C0JobCreating, "client must observe the resulting state");
}

// Deliberately bypasses C012ServerChannel: a real server signs requests and responses with
// the same shared secret, so a client/server secret mismatch fails the *request* and, by
// design, the server sends no response frame at all for an unauthenticated request -- the
// client would then hang forever waiting for a reply that structurally cannot arrive (this
// is exactly what the 10-second safety timeout above caught: OperationCanceledException,
// not C012FramingException, in the version of this test that made that mistake). To test
// "client rejects a wrongly-signed response" specifically, the request must be left
// unanswered by any real server and instead answered by hand with a forged response signed
// under a different secret than the client holds.
static void ClientRejectsResponseWithWrongHmac()
{
    using C012SessionSecret clientSecret = C012SessionSecret.Generate();
    byte[] wrongSecret = System.Security.Cryptography.RandomNumberGenerator.GetBytes(32);
    Guid sessionId = Guid.NewGuid();

    using DuplexPair pair = DuplexPair.Create();
    var client = new C012ClientChannel(pair.ClientStream, clientSecret, sessionId);

    Task<C012TransitionResult> clientTask = client.SendAsync(C012Control.C0, C012RequestType.Start, ShortTestTimeout());

    byte[]? requestFrame = C012FrameCodec.ReadFrameAsync(pair.ServerStream, ShortTestTimeout()).GetAwaiter().GetResult();
    Assert(requestFrame is not null, "setup: the client must have written a request");

    string forgedHmac = C012MessageAuthenticator.SignResponse(
        wrongSecret, C012WireSchema.Version, sessionId, 1, true, C012State.C0JobCreating, C012RejectionReason.None);
    var forgedResponse = new C012WireResponse(
        C012WireSchema.Version, sessionId, 1, true, C012State.C0JobCreating, C012RejectionReason.None, forgedHmac);
    byte[] forgedResponseBytes = JsonSerializer.SerializeToUtf8Bytes(forgedResponse, C012WireJsonOptions.Instance);
    C012FrameCodec.WriteFrameAsync(pair.ServerStream, forgedResponseBytes, ShortTestTimeout()).GetAwaiter().GetResult();

    bool threw = false;
    try
    {
        clientTask.GetAwaiter().GetResult();
    }
    catch (C012FramingException)
    {
        threw = true;
    }

    Assert(threw, "the client must refuse a response signed with a secret it does not hold");
}

static void ClientKeepsSameSequenceSlotAfterServerRejection()
{
    using C012SessionSecret secret = C012SessionSecret.Generate();
    Guid sessionId = Guid.NewGuid();
    var sequencer = new C012RequestSequencer(sessionId);

    using DuplexPair firstPair = DuplexPair.Create();
    var serverForFirst = new C012ServerChannel(firstPair.ServerStream, secret, sequencer);
    var client = new C012ClientChannel(firstPair.ClientStream, secret, sessionId);

    Task<C012TransitionResult> firstClientTask =
        client.SendAsync(C012Control.C2, C012RequestType.SubmitConfig, ShortTestTimeout());
    _ = serverForFirst.ProcessNextRequestAsync(ShortTestTimeout()).GetAwaiter().GetResult();
    C012TransitionResult firstClientResult = firstClientTask.GetAwaiter().GetResult();
    Assert(!firstClientResult.Accepted, "premature C2 must be rejected");

    using DuplexPair secondPair = DuplexPair.Create();
    var serverForSecond = new C012ServerChannel(secondPair.ServerStream, secret, sequencer);
    var clientOnSecondPipe = new C012ClientChannel(secondPair.ClientStream, secret, sessionId);
    // A fresh C012ClientChannel starts at sequence 1 again; since the first (rejected) call
    // never advanced the shared sequencer's counter either, sequence 1 is still legal here.
    Task<C012TransitionResult> secondClientTask =
        clientOnSecondPipe.SendAsync(C012Control.C0, C012RequestType.Start, ShortTestTimeout());
    _ = serverForSecond.ProcessNextRequestAsync(ShortTestTimeout()).GetAwaiter().GetResult();
    C012TransitionResult secondClientResult = secondClientTask.GetAwaiter().GetResult();

    Assert(secondClientResult.Accepted, "sequence slot 1 must still be usable after the earlier rejection");
}

// ---- B3: duplex stream EOF/closure ----

static void ChannelStreamReadReturnsZeroAfterWriterCompletes()
{
    Channel<byte[]> channel = Channel.CreateUnbounded<byte[]>();
    channel.Writer.Complete();

    var stream = new ChannelStream(channel.Reader, channel.Writer);
    int read = stream.ReadAsync(new byte[4], CancellationToken.None).GetAwaiter().GetResult();
    Assert(read == 0, "reading an already-completed, empty channel must return 0, not hang");
}

static void DuplexPairDisposeCompletesBothChannelsWithoutHanging()
{
    var pair = DuplexPair.Create();
    pair.Dispose();

    int serverRead = pair.ServerStream.ReadAsync(new byte[4], CancellationToken.None).GetAwaiter().GetResult();
    int clientRead = pair.ClientStream.ReadAsync(new byte[4], CancellationToken.None).GetAwaiter().GetResult();
    Assert(serverRead == 0, "reading the server side after Dispose must return 0, not hang");
    Assert(clientRead == 0, "reading the client side after Dispose must return 0, not hang");
}

// ---- B4.1: orchestrating processor ----

static void OrchestratingProcessorC0SuccessCallsLauncherInOrderAndReachesC0Retained()
{
    var launcher = new FakeRootProcessLauncher();
    Guid sessionId = Guid.NewGuid();
    var sequencer = new C012RequestSequencer(sessionId);
    var processor = new C012OrchestratingProcessor(sequencer, launcher);

    C012TransitionResult result = processor.Apply(
        new C012RequestEnvelope(sessionId, 1, C012Control.C0, C012RequestType.Start));

    Assert(result.Accepted, "C0 must be accepted when every launcher call succeeds");
    Assert(result.ResultingState == C012State.C0Retained, "C0 success must reach C0Retained");
    Assert(
        launcher.Calls.SequenceEqual(new[]
        {
            nameof(IC012RootProcessLauncher.CreateJob),
            nameof(IC012RootProcessLauncher.LaunchSuspendedRoot),
            nameof(IC012RootProcessLauncher.AssignRootToJob),
            nameof(IC012RootProcessLauncher.ResumeRoot),
        }),
        "C0 must call exactly these launcher operations in this order");
}

static void OrchestratingProcessorC0FailsWhenCreateJobThrows()
{
    var launcher = new FakeRootProcessLauncher
    {
        CreateJobFault = () => throw new InvalidOperationException("simulated"),
    };
    Guid sessionId = Guid.NewGuid();
    var sequencer = new C012RequestSequencer(sessionId);
    var processor = new C012OrchestratingProcessor(sequencer, launcher);

    C012TransitionResult result = processor.Apply(
        new C012RequestEnvelope(sessionId, 1, C012Control.C0, C012RequestType.Start));

    Assert(!result.Accepted, "a CreateJob failure must not be accepted");
    Assert(result.ResultingState == C012State.FailedClosed, "a CreateJob failure must fail closed");
    Assert(result.Reason == C012RejectionReason.OperationFailed, "reason must be OperationFailed");
    Assert(
        launcher.Calls.SequenceEqual(new[] { nameof(IC012RootProcessLauncher.CreateJob) }),
        "no further launcher call may happen after CreateJob fails");
    Assert(launcher.TeardownJobCallCount == 0, "nothing was created, so nothing may be torn down");
}

static void OrchestratingProcessorC0FailsWhenLaunchRootThrows()
{
    var launcher = new FakeRootProcessLauncher
    {
        LaunchSuspendedRootFault = () => throw new InvalidOperationException("simulated"),
    };
    Guid sessionId = Guid.NewGuid();
    var sequencer = new C012RequestSequencer(sessionId);
    var processor = new C012OrchestratingProcessor(sequencer, launcher);

    C012TransitionResult result = processor.Apply(
        new C012RequestEnvelope(sessionId, 1, C012Control.C0, C012RequestType.Start));

    Assert(!result.Accepted && result.ResultingState == C012State.FailedClosed, "a LaunchSuspendedRoot failure must fail closed");
    Assert(result.Reason == C012RejectionReason.OperationFailed, "reason must be OperationFailed");
    Assert(
        !launcher.Calls.Contains(nameof(IC012RootProcessLauncher.AssignRootToJob)),
        "AssignRootToJob must never run once LaunchSuspendedRoot has failed");
    Assert(launcher.TeardownJobCallCount == 1, "the already-created Job must be torn down");
}

static void OrchestratingProcessorC0FailsWhenAssignRootThrows()
{
    var launcher = new FakeRootProcessLauncher
    {
        AssignRootToJobFault = () => throw new InvalidOperationException("simulated"),
    };
    Guid sessionId = Guid.NewGuid();
    var sequencer = new C012RequestSequencer(sessionId);
    var processor = new C012OrchestratingProcessor(sequencer, launcher);

    C012TransitionResult result = processor.Apply(
        new C012RequestEnvelope(sessionId, 1, C012Control.C0, C012RequestType.Start));

    Assert(!result.Accepted && result.ResultingState == C012State.FailedClosed, "an AssignRootToJob failure must fail closed");
    Assert(
        !launcher.Calls.Contains(nameof(IC012RootProcessLauncher.ResumeRoot)),
        "ResumeRoot must never run once AssignRootToJob has failed");
    Assert(launcher.TeardownJobCallCount == 1, "the already-created Job must be torn down");
}

static void OrchestratingProcessorC0FailsWhenResumeRootThrows()
{
    var launcher = new FakeRootProcessLauncher
    {
        ResumeRootFault = () => throw new InvalidOperationException("simulated"),
    };
    Guid sessionId = Guid.NewGuid();
    var sequencer = new C012RequestSequencer(sessionId);
    var processor = new C012OrchestratingProcessor(sequencer, launcher);

    C012TransitionResult result = processor.Apply(
        new C012RequestEnvelope(sessionId, 1, C012Control.C0, C012RequestType.Start));

    Assert(!result.Accepted && result.ResultingState == C012State.FailedClosed, "a ResumeRoot failure must fail closed");
    Assert(launcher.TeardownJobCallCount == 1, "the already-created Job must be torn down");
}

static void OrchestratingProcessorC1SuccessReachesC1Retained()
{
    var launcher = new FakeRootProcessLauncher();
    Guid sessionId = Guid.NewGuid();
    var sequencer = new C012RequestSequencer(sessionId);
    var processor = new C012OrchestratingProcessor(sequencer, launcher);
    DriveC0ToRetained(processor, sessionId);

    C012TransitionResult result = processor.Apply(
        new C012RequestEnvelope(sessionId, 2, C012Control.C1, C012RequestType.Query));

    Assert(result.Accepted && result.ResultingState == C012State.C1Retained, "C1 success must reach C1Retained");
    Assert(launcher.Calls.Contains(nameof(IC012RootProcessLauncher.VerifyRootAlive)), "C1 must verify root liveness");
}

static void OrchestratingProcessorC1FailsRootDiedWhenVerifyRootAliveReturnsFalse()
{
    var launcher = new FakeRootProcessLauncher { VerifyRootAliveResult = false };
    Guid sessionId = Guid.NewGuid();
    var sequencer = new C012RequestSequencer(sessionId);
    var processor = new C012OrchestratingProcessor(sequencer, launcher);
    DriveC0ToRetained(processor, sessionId);

    C012TransitionResult result = processor.Apply(
        new C012RequestEnvelope(sessionId, 2, C012Control.C1, C012RequestType.Query));

    Assert(
        !result.Accepted && result.ResultingState == C012State.FailedClosed,
        "a negative liveness check must fail closed");
    Assert(
        result.Reason == C012RejectionReason.RootProcessDied,
        "a completed check that determined the root is gone must report RootProcessDied, not OperationFailed");
}

static void OrchestratingProcessorC1FailsOperationWhenVerifyRootAliveThrows()
{
    var launcher = new FakeRootProcessLauncher
    {
        VerifyRootAliveFault = () => throw new InvalidOperationException("simulated"),
    };
    Guid sessionId = Guid.NewGuid();
    var sequencer = new C012RequestSequencer(sessionId);
    var processor = new C012OrchestratingProcessor(sequencer, launcher);
    DriveC0ToRetained(processor, sessionId);

    C012TransitionResult result = processor.Apply(
        new C012RequestEnvelope(sessionId, 2, C012Control.C1, C012RequestType.Query));

    Assert(
        !result.Accepted && result.ResultingState == C012State.FailedClosed,
        "a liveness check that could not run must fail closed");
    Assert(
        result.Reason == C012RejectionReason.OperationFailed,
        "a check that could not complete must report OperationFailed, not RootProcessDied");
}

static void OrchestratingProcessorC2FailsWhenLaunchSubmitterThrows()
{
    var launcher = new FakeRootProcessLauncher
    {
        LaunchSuspendedSubmitterFault = () => throw new InvalidOperationException("simulated"),
    };
    Guid sessionId = Guid.NewGuid();
    var sequencer = new C012RequestSequencer(sessionId);
    var processor = new C012OrchestratingProcessor(sequencer, launcher);
    DriveC0ToRetained(processor, sessionId);
    DriveC1ToRetained(processor, sessionId);

    C012TransitionResult result = processor.Apply(
        new C012RequestEnvelope(sessionId, 3, C012Control.C2, C012RequestType.SubmitConfig));

    Assert(!result.Accepted && result.ResultingState == C012State.FailedClosed, "a LaunchSuspendedSubmitter failure must fail closed");
    Assert(
        !launcher.Calls.Contains(nameof(IC012RootProcessLauncher.AssignSubmitterToJob)),
        "AssignSubmitterToJob must never run once LaunchSuspendedSubmitter has failed");
}

static void OrchestratingProcessorC2FailsWhenAssignSubmitterThrows()
{
    var launcher = new FakeRootProcessLauncher
    {
        AssignSubmitterToJobFault = () => throw new InvalidOperationException("simulated"),
    };
    Guid sessionId = Guid.NewGuid();
    var sequencer = new C012RequestSequencer(sessionId);
    var processor = new C012OrchestratingProcessor(sequencer, launcher);
    DriveC0ToRetained(processor, sessionId);
    DriveC1ToRetained(processor, sessionId);

    C012TransitionResult result = processor.Apply(
        new C012RequestEnvelope(sessionId, 3, C012Control.C2, C012RequestType.SubmitConfig));

    Assert(!result.Accepted && result.ResultingState == C012State.FailedClosed, "an AssignSubmitterToJob failure must fail closed");
    Assert(
        !launcher.Calls.Contains(nameof(IC012RootProcessLauncher.VerifySubmitterSameJob)),
        "VerifySubmitterSameJob must never run once AssignSubmitterToJob has failed");
}

static void OrchestratingProcessorC2FailsWhenVerifySameJobReturnsFalse()
{
    var launcher = new FakeRootProcessLauncher { VerifySubmitterSameJobResult = false };
    Guid sessionId = Guid.NewGuid();
    var sequencer = new C012RequestSequencer(sessionId);
    var processor = new C012OrchestratingProcessor(sequencer, launcher);
    DriveC0ToRetained(processor, sessionId);
    DriveC1ToRetained(processor, sessionId);

    C012TransitionResult result = processor.Apply(
        new C012RequestEnvelope(sessionId, 3, C012Control.C2, C012RequestType.SubmitConfig));

    Assert(
        !result.Accepted && result.ResultingState == C012State.FailedClosed,
        "the submitter must never be resumed if it is not confirmed to be in the same Job");
    Assert(result.Reason == C012RejectionReason.OperationFailed, "reason must be OperationFailed");
    Assert(
        !launcher.Calls.Contains(nameof(IC012RootProcessLauncher.ResumeAndAwaitSubmitter)),
        "the submitter must not be resumed once same-job verification fails");
}

static void OrchestratingProcessorC2FailsWhenVerifySameJobThrows()
{
    var launcher = new FakeRootProcessLauncher
    {
        VerifySubmitterSameJobFault = () => throw new InvalidOperationException("simulated"),
    };
    Guid sessionId = Guid.NewGuid();
    var sequencer = new C012RequestSequencer(sessionId);
    var processor = new C012OrchestratingProcessor(sequencer, launcher);
    DriveC0ToRetained(processor, sessionId);
    DriveC1ToRetained(processor, sessionId);

    C012TransitionResult result = processor.Apply(
        new C012RequestEnvelope(sessionId, 3, C012Control.C2, C012RequestType.SubmitConfig));

    Assert(!result.Accepted && result.ResultingState == C012State.FailedClosed, "a same-job check that could not run must fail closed");
}

static void OrchestratingProcessorC2SuccessReachesTerminatedAndTearsDownExactlyOnce()
{
    var launcher = new FakeRootProcessLauncher();
    Guid sessionId = Guid.NewGuid();
    var sequencer = new C012RequestSequencer(sessionId);
    var processor = new C012OrchestratingProcessor(sequencer, launcher);
    DriveC0ToRetained(processor, sessionId);
    DriveC1ToRetained(processor, sessionId);

    C012TransitionResult result = processor.Apply(
        new C012RequestEnvelope(sessionId, 3, C012Control.C2, C012RequestType.SubmitConfig));

    Assert(result.Accepted && result.ResultingState == C012State.Terminated, "C2 success must reach Terminated");
    Assert(launcher.TeardownJobCallCount == 1, "the Job must be torn down exactly once on success");
    Assert(
        launcher.Calls.Contains(nameof(IC012RootProcessLauncher.ResumeAndAwaitSubmitter)),
        "the submitter must be resumed and awaited once verified to be in the same Job");
}

static void OrchestratingProcessorC2FailsWhenTeardownThrowsAndDoesNotDoubleTeardown()
{
    var launcher = new FakeRootProcessLauncher
    {
        TeardownJobFault = () => throw new InvalidOperationException("simulated"),
    };
    Guid sessionId = Guid.NewGuid();
    var sequencer = new C012RequestSequencer(sessionId);
    var processor = new C012OrchestratingProcessor(sequencer, launcher);
    DriveC0ToRetained(processor, sessionId);
    DriveC1ToRetained(processor, sessionId);

    C012TransitionResult result = processor.Apply(
        new C012RequestEnvelope(sessionId, 3, C012Control.C2, C012RequestType.SubmitConfig));

    Assert(
        !result.Accepted && result.ResultingState == C012State.FailedClosed,
        "a teardown failure must not be reported as a successful Terminated session");
    Assert(launcher.TeardownJobCallCount == 1, "teardown must be attempted exactly once, never retried");
}

static void OrchestratingProcessorNeverCallsLauncherOnceFailedClosed()
{
    var launcher = new FakeRootProcessLauncher();
    Guid sessionId = Guid.NewGuid();
    var sequencer = new C012RequestSequencer(sessionId);
    var processor = new C012OrchestratingProcessor(sequencer, launcher);
    DriveC0ToRetained(processor, sessionId);

    // Simulate a host-driven watchdog timeout applied directly to the shared sequencer,
    // bypassing the processor entirely -- exactly as a real host's timer would.
    sequencer.ApplyInternal(C012Trigger.Timeout);
    launcher.Calls.Clear();

    C012TransitionResult result = processor.Apply(
        new C012RequestEnvelope(sessionId, 2, C012Control.C1, C012RequestType.Query));

    Assert(!result.Accepted && result.ResultingState == C012State.FailedClosed, "a request after FailedClosed must be rejected");
    Assert(launcher.Calls.Count == 0, "no launcher call may happen once the session is FailedClosed");
}

static void OrchestratingProcessorRejectsIllegalRequestWithoutTouchingLauncher()
{
    var launcher = new FakeRootProcessLauncher();
    Guid sessionId = Guid.NewGuid();
    var sequencer = new C012RequestSequencer(sessionId);
    var processor = new C012OrchestratingProcessor(sequencer, launcher);

    // C2 before C0/C1: the gate must reject this before the launcher is ever touched.
    C012TransitionResult result = processor.Apply(
        new C012RequestEnvelope(sessionId, 1, C012Control.C2, C012RequestType.SubmitConfig));

    Assert(!result.Accepted, "C2 before C0/C1 must be rejected");
    Assert(launcher.Calls.Count == 0, "an illegal request must never reach the launcher");
}

static void DriveC0ToRetained(C012OrchestratingProcessor processor, Guid sessionId)
{
    C012TransitionResult result = processor.Apply(
        new C012RequestEnvelope(sessionId, 1, C012Control.C0, C012RequestType.Start));
    Assert(result.Accepted && result.ResultingState == C012State.C0Retained, "setup: C0 must reach C0Retained");
}

static void DriveC1ToRetained(C012OrchestratingProcessor processor, Guid sessionId)
{
    C012TransitionResult result = processor.Apply(
        new C012RequestEnvelope(sessionId, 2, C012Control.C1, C012RequestType.Query));
    Assert(result.Accepted && result.ResultingState == C012State.C1Retained, "setup: C1 must reach C1Retained");
}

// ---- shared helpers ----

// Builds a fresh C012ServerChannel bound to a single MemoryStream pre-loaded with one
// crafted, signed request, invokes it, then (for a processed request) seeks back to right
// after the request bytes to read and parse whatever response the server wrote there. One
// MemoryStream is enough because the server only ever reads then writes sequentially -- no
// real duplex concurrency is needed to exercise C012ServerChannel in isolation.
static void ExchangeSingleRequest(
    C012SessionSecret serverSecret, C012RequestSequencer sequencer, C012SessionSecret signingSecret,
    Guid sessionId, long sequenceNumber, C012Control control, C012RequestType requestType,
    out C012ChannelOutcome outcome, out C012WireResponse? response, bool expectNoResponse = false)
{
    string hmac = C012MessageAuthenticator.SignRequest(
        signingSecret.Value, C012WireSchema.Version, sessionId, sequenceNumber, control, requestType);
    var request = new C012WireRequest(C012WireSchema.Version, sessionId, sequenceNumber, control, requestType, hmac);
    byte[] requestBytes = JsonSerializer.SerializeToUtf8Bytes(request, C012WireJsonOptions.Instance);

    var stream = new MemoryStream();
    C012FrameCodec.WriteFrameAsync(stream, requestBytes, CancellationToken.None).GetAwaiter().GetResult();
    long requestEnd = stream.Position;
    stream.Position = 0;

    var server = new C012ServerChannel(stream, serverSecret, sequencer);
    outcome = server.ProcessNextRequestAsync(CancellationToken.None).GetAwaiter().GetResult();

    response = null;
    if (!expectNoResponse && outcome == C012ChannelOutcome.RequestProcessed)
    {
        stream.Position = requestEnd;
        byte[]? responseFrame = C012FrameCodec.ReadFrameAsync(stream, CancellationToken.None).GetAwaiter().GetResult();
        Assert(responseFrame is not null, "a processed request must be followed by a response frame");
        response = JsonSerializer.Deserialize<C012WireResponse>(responseFrame!, C012WireJsonOptions.Instance);
    }
}

static string CreateTemporaryDirectory()
{
    string path = Path.Combine(Path.GetTempPath(), $"c012-secret-test-{Guid.NewGuid():N}");
    Directory.CreateDirectory(path);
    return path;
}

// Safety net for the client/server round-trip tests: if the deterministic ordering they
// rely on is ever wrong, this turns a hang into a fast, clearly-reported failure instead of
// exhausting the CI job's 25-minute ceiling.
static CancellationToken ShortTestTimeout() => new CancellationTokenSource(TimeSpan.FromSeconds(10)).Token;

static void Assert(bool condition, string message)
{
    if (!condition)
    {
        throw new InvalidOperationException(message);
    }
}

// ---- fakes and test-only types (must follow every top-level statement/local function
// above: C# requires all top-level statements in a file to precede any type declaration) ----

internal sealed class FakeRootProcessLauncher : IC012RootProcessLauncher
{
    public List<string> Calls { get; } = new();

    public Action? CreateJobFault { get; set; }

    public Action? LaunchSuspendedRootFault { get; set; }

    public Action? AssignRootToJobFault { get; set; }

    public Action? ResumeRootFault { get; set; }

    public bool VerifyRootAliveResult { get; set; } = true;

    public Action? VerifyRootAliveFault { get; set; }

    public Action? LaunchSuspendedSubmitterFault { get; set; }

    public Action? AssignSubmitterToJobFault { get; set; }

    public bool VerifySubmitterSameJobResult { get; set; } = true;

    public Action? VerifySubmitterSameJobFault { get; set; }

    public Action? ResumeAndAwaitSubmitterFault { get; set; }

    public Action? TeardownJobFault { get; set; }

    public int TeardownJobCallCount { get; private set; }

    public C012JobToken CreateJob()
    {
        Calls.Add(nameof(CreateJob));
        CreateJobFault?.Invoke();
        return new C012JobToken(new object());
    }

    public C012ProcessToken LaunchSuspendedRoot(C012JobToken job)
    {
        Calls.Add(nameof(LaunchSuspendedRoot));
        LaunchSuspendedRootFault?.Invoke();
        return new C012ProcessToken(new object());
    }

    public void AssignRootToJob(C012JobToken job, C012ProcessToken root)
    {
        Calls.Add(nameof(AssignRootToJob));
        AssignRootToJobFault?.Invoke();
    }

    public void ResumeRoot(C012ProcessToken root)
    {
        Calls.Add(nameof(ResumeRoot));
        ResumeRootFault?.Invoke();
    }

    public bool VerifyRootAlive(C012JobToken job, C012ProcessToken root)
    {
        Calls.Add(nameof(VerifyRootAlive));
        VerifyRootAliveFault?.Invoke();
        return VerifyRootAliveResult;
    }

    public C012ProcessToken LaunchSuspendedSubmitter(C012JobToken job)
    {
        Calls.Add(nameof(LaunchSuspendedSubmitter));
        LaunchSuspendedSubmitterFault?.Invoke();
        return new C012ProcessToken(new object());
    }

    public void AssignSubmitterToJob(C012JobToken job, C012ProcessToken submitter)
    {
        Calls.Add(nameof(AssignSubmitterToJob));
        AssignSubmitterToJobFault?.Invoke();
    }

    public bool VerifySubmitterSameJob(C012JobToken job, C012ProcessToken submitter)
    {
        Calls.Add(nameof(VerifySubmitterSameJob));
        VerifySubmitterSameJobFault?.Invoke();
        return VerifySubmitterSameJobResult;
    }

    public void ResumeAndAwaitSubmitter(C012JobToken job, C012ProcessToken submitter)
    {
        Calls.Add(nameof(ResumeAndAwaitSubmitter));
        ResumeAndAwaitSubmitterFault?.Invoke();
    }

    public void TeardownJob(C012JobToken job)
    {
        Calls.Add(nameof(TeardownJob));
        TeardownJobCallCount++;
        TeardownJobFault?.Invoke();
    }
}

internal sealed class OneByteAtATimeStream : Stream
{
    private readonly MemoryStream _inner;

    public OneByteAtATimeStream(byte[] data)
    {
        _inner = new MemoryStream(data);
    }

    public override bool CanRead => true;

    public override bool CanSeek => false;

    public override bool CanWrite => false;

    public override long Length => throw new NotSupportedException();

    public override long Position
    {
        get => throw new NotSupportedException();
        set => throw new NotSupportedException();
    }

    public override ValueTask<int> ReadAsync(Memory<byte> buffer, CancellationToken cancellationToken = default) =>
        buffer.IsEmpty ? ValueTask.FromResult(0) : _inner.ReadAsync(buffer[..1], cancellationToken);

    public override int Read(byte[] buffer, int offset, int count) => throw new NotSupportedException();

    public override void Write(byte[] buffer, int offset, int count) => throw new NotSupportedException();

    public override long Seek(long offset, SeekOrigin origin) => throw new NotSupportedException();

    public override void SetLength(long value) => throw new NotSupportedException();

    public override void Flush()
    {
    }

    protected override void Dispose(bool disposing)
    {
        if (disposing)
        {
            _inner.Dispose();
        }

        base.Dispose(disposing);
    }
}

// One direction of an in-process duplex stream, backed by an unbounded
// System.Threading.Channels.Channel<byte[]>. Genuinely async (WaitToReadAsync suspends and
// resumes via the channel's own continuation, never blocking a thread), entirely in-memory,
// with no OS pipe involved -- anonymous pipes were tried first and dropped because Windows
// anonymous pipes do not support true overlapped/async I/O, which deadlocked the
// server/client round trip below (the "async" read blocked the calling thread before the
// paired write could ever run).
internal sealed class ChannelStream : Stream
{
    private readonly ChannelReader<byte[]> _reader;
    private readonly ChannelWriter<byte[]> _writer;
    private ReadOnlyMemory<byte> _pending;

    public ChannelStream(ChannelReader<byte[]> reader, ChannelWriter<byte[]> writer)
    {
        _reader = reader;
        _writer = writer;
    }

    public override bool CanRead => true;

    public override bool CanSeek => false;

    public override bool CanWrite => true;

    public override long Length => throw new NotSupportedException();

    public override long Position
    {
        get => throw new NotSupportedException();
        set => throw new NotSupportedException();
    }

    public override async ValueTask<int> ReadAsync(Memory<byte> buffer, CancellationToken cancellationToken = default)
    {
        if (_pending.IsEmpty)
        {
            if (!await _reader.WaitToReadAsync(cancellationToken).ConfigureAwait(false))
            {
                return 0;
            }

            if (!_reader.TryRead(out byte[]? chunk) || chunk is null)
            {
                return 0;
            }

            _pending = chunk;
        }

        int toCopy = Math.Min(buffer.Length, _pending.Length);
        _pending.Span[..toCopy].CopyTo(buffer.Span);
        _pending = _pending[toCopy..];
        return toCopy;
    }

    public override ValueTask WriteAsync(ReadOnlyMemory<byte> buffer, CancellationToken cancellationToken = default)
    {
        ValueTask writeTask = _writer.WriteAsync(buffer.ToArray(), cancellationToken);
        return writeTask;
    }

    public override Task FlushAsync(CancellationToken cancellationToken) => Task.CompletedTask;

    public override int Read(byte[] buffer, int offset, int count) => throw new NotSupportedException();

    public override void Write(byte[] buffer, int offset, int count) => throw new NotSupportedException();

    public override long Seek(long offset, SeekOrigin origin) => throw new NotSupportedException();

    public override void SetLength(long value) => throw new NotSupportedException();

    public override void Flush()
    {
    }

    // Completes this stream's own writer so the peer's ReadAsync -- pending or future --
    // observes a clean 0/EOF via WaitToReadAsync returning false, instead of waiting
    // forever. TryComplete (not Complete) is idempotent: safe if Dispose runs more than
    // once.
    protected override void Dispose(bool disposing)
    {
        if (disposing)
        {
            _writer.TryComplete();
        }

        base.Dispose(disposing);
    }
}

internal sealed class DuplexPair : IDisposable
{
    private DuplexPair(Stream serverStream, Stream clientStream)
    {
        ServerStream = serverStream;
        ClientStream = clientStream;
    }

    public Stream ServerStream { get; }

    public Stream ClientStream { get; }

    public static DuplexPair Create()
    {
        Channel<byte[]> clientToServer = Channel.CreateUnbounded<byte[]>();
        Channel<byte[]> serverToClient = Channel.CreateUnbounded<byte[]>();

        var serverStream = new ChannelStream(clientToServer.Reader, serverToClient.Writer);
        var clientStream = new ChannelStream(serverToClient.Reader, clientToServer.Writer);
        return new DuplexPair(serverStream, clientStream);
    }

    // Disposing both streams completes both writers, so a read on either side after
    // Dispose observes EOF rather than hanging -- verified by
    // duplex_pair_dispose_completes_both_channels_without_hanging.
    public void Dispose()
    {
        ServerStream.Dispose();
        ClientStream.Dispose();
    }
}

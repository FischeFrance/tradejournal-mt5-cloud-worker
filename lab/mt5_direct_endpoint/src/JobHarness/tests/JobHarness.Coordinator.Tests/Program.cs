using System.Diagnostics;
using System.IO.Pipes;
using System.Reflection;
using System.Security.Cryptography;
using System.Text.Json;
using System.Threading.Channels;
using TradeJournal.Lab.JobHarness.Coordinator;

// Self-invocation targets for C012InnocuousRootProcessLauncher's Windows-only smoke tests
// below: the launcher re-invokes this same executable as its root/submitter, so this process
// must understand these two flags and behave innocuously (long-lived vs. immediate exit)
// instead of running the whole test suite again. Must run before the test harness below --
// C# requires every top-level statement in a file to precede any type declaration, and these
// checks must in turn run before "var tests = ..." starts building the harness.
if (args is ["--innocent-sleeper"])
{
    System.Threading.Thread.Sleep(TimeSpan.FromSeconds(30));
    return 0;
}

if (args is ["--innocent-exit-zero"])
{
    return 0;
}

// B4.4: same idea as the two flags above, but each also writes its own PID and kernel start
// time to a caller-supplied ready file before doing anything else -- the only way a test
// process can learn the real PID of a root/submitter launched by a separate, real host
// process it does not share memory with. Format matches JobHarness.SmokeTests'
// --innocent-child ("pid|start-time-ticks") for consistency, though this is a private,
// project-local convention -- nothing reads across the two test projects.
if (args is ["--innocent-ready-then-sleep", var readyPathForSleep])
{
    WriteReadyRecord(readyPathForSleep);
    System.Threading.Thread.Sleep(TimeSpan.FromSeconds(30));
    return 0;
}

if (args is ["--innocent-ready-then-exit", var readyPathForExit])
{
    WriteReadyRecord(readyPathForExit);
    return 0;
}

// B4.4: the only way a test spawns a genuinely separate Windows process that hosts a real
// C012 session with the real (innocuous-only) launcher. c012-host start itself never gains
// this capability -- Program.cs (the production JobHarness.exe entry point) has no branch
// like this one and never will; this exists solely inside the test executable, reusing the
// already-existing, already test-only 5-argument C012HostCli.Run overload from B4.3/B4.4.
if (args.Length > 0 && args[0] == "--run-real-host-for-testing")
{
    string sessionDirForRealHost = RequireNamedArg(args, "--session-dir");
    string executableForRealHost = RequireNamedArg(args, "--executable");
    string expectedSha256ForRealHost = RequireNamedArg(args, "--expected-sha256");
    string rootReadyPathForRealHost = RequireNamedArg(args, "--root-ready-path");
    int idleTimeoutSecondsForRealHost = int.Parse(
        RequireNamedArg(args, "--idle-timeout-seconds"), System.Globalization.CultureInfo.InvariantCulture);

    var realHostLauncher = new C012InnocuousRootProcessLauncher(
        executableForRealHost,
        expectedSha256ForRealHost,
        SelfInvocationArguments(executableForRealHost, "--innocent-ready-then-sleep", rootReadyPathForRealHost),
        SelfInvocationArguments(executableForRealHost, "--innocent-exit-zero"),
        TimeSpan.FromSeconds(10));

    return C012HostCli.Run(
        ["--session-dir", sessionDirForRealHost],
        Console.Out,
        Console.Error,
        realHostLauncher,
        TimeSpan.FromSeconds(idleTimeoutSecondsForRealHost));
}

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

    // B4.4 fix: the host-loop-to-orchestrator teardown seam
    ("orchestrating_processor_fail_from_host_tears_down_after_c0_and_reaches_failed_closed", OrchestratingProcessorFailFromHostTearsDownAfterC0AndReachesFailedClosed),
    ("orchestrating_processor_fail_from_host_is_idempotent_if_called_twice", OrchestratingProcessorFailFromHostIsIdempotentIfCalledTwice),
    ("orchestrating_processor_fail_from_host_rejects_client_originated_triggers", OrchestratingProcessorFailFromHostRejectsClientOriginatedTriggers),

    // B4.2: session paths
    ("session_paths_derive_pipe_name_is_deterministic_and_unique_per_session", SessionPathsDerivePipeNameIsDeterministicAndUniquePerSession),
    ("session_paths_write_and_read_session_id_round_trips", SessionPathsWriteAndReadSessionIdRoundTrips),
    ("session_paths_write_session_id_refuses_to_overwrite", SessionPathsWriteSessionIdRefusesToOverwrite),
    ("session_paths_inspect_reports_missing_directory", SessionPathsInspectReportsMissingDirectory),
    ("session_paths_inspect_reports_empty_directory", SessionPathsInspectReportsEmptyDirectory),
    ("session_paths_inspect_reports_full_session", SessionPathsInspectReportsFullSession),

    // B4.2: placeholder launcher
    ("not_implemented_launcher_causes_c0_to_fail_closed_via_operation_failed", NotImplementedLauncherCausesC0ToFailClosedViaOperationFailed),

    // B4.2: client CLI
    ("client_cli_status_reports_missing_directory_without_claiming_liveness", ClientCliStatusReportsMissingDirectoryWithoutClaimingLiveness),
    ("client_cli_status_reports_full_session_without_claiming_liveness", ClientCliStatusReportsFullSessionWithoutClaimingLiveness),
    ("client_cli_requires_session_dir_flag", ClientCliRequiresSessionDirFlag),
    ("client_cli_rejects_unknown_verb", ClientCliRejectsUnknownVerb),

    // B4.2: host CLI
    ("host_cli_requires_session_dir_flag", HostCliRequiresSessionDirFlag),
    ("host_cli_refuses_when_session_dir_missing", HostCliRefusesWhenSessionDirMissing),
    ("host_cli_refuses_when_session_already_exists", HostCliRefusesWhenSessionAlreadyExists),

    // c012-host start-innocuous: narrow, explicitly authorized exception (see AGENTS.md)
    ("host_cli_start_innocuous_requires_target_flag", HostCliStartInnocuousRequiresTargetFlag),
    ("host_cli_start_innocuous_rejects_unknown_target", HostCliStartInnocuousRejectsUnknownTarget),

    // B4.2: real Named Pipe (Windows-only)
    ("host_and_client_real_named_pipe_round_trip_reaches_failed_closed_via_placeholder_launcher", HostAndClientRealNamedPipeRoundTripReachesFailedClosedViaPlaceholderLauncher),
    ("client_cli_fails_cleanly_when_no_pipe_is_listening", ClientCliFailsCleanlyWhenNoPipeIsListening),

    // B4.3: real Windows launcher (Windows-only)
    ("innocuous_launcher_c0_through_c2_reaches_terminated_and_root_is_gone_after_teardown", InnocuousLauncherC0ThroughC2ReachesTerminatedAndRootIsGoneAfterTeardown),
    ("innocuous_launcher_end_to_end_over_real_named_pipe_reaches_terminated", InnocuousLauncherEndToEndOverRealNamedPipeReachesTerminated),
    ("innocuous_launcher_closing_last_job_handle_kills_root_via_kill_on_job_close", InnocuousLauncherClosingLastJobHandleKillsRootViaKillOnJobClose),
    ("innocuous_launcher_constructor_refuses_metatrader_like_executable_name", InnocuousLauncherConstructorRefusesMetaTraderLikeExecutableName),
    ("host_cli_start_innocuous_with_self_sleeper_target_reaches_terminated_over_real_named_pipe", HostCliStartInnocuousWithSelfSleeperTargetReachesTerminatedOverRealNamedPipe),

    // B4.2 fix: session sequence cursor continuity across separate c012-client processes
    // (Windows-only)
    ("session_sequence_cursor_advances_one_two_three_on_accepted_responses", SessionSequenceCursorAdvancesOneTwoThreeOnAcceptedResponses),
    ("session_sequence_cursor_concurrent_acquire_is_rejected_as_busy", SessionSequenceCursorConcurrentAcquireIsRejectedAsBusy),
    ("session_sequence_cursor_write_ahead_pending_survives_reacquire", SessionSequenceCursorWriteAheadPendingSurvivesReacquire),
    ("session_sequence_cursor_clean_rejection_leaves_same_sequence_reusable", SessionSequenceCursorCleanRejectionLeavesSameSequenceReusable),
    ("client_cli_refuses_when_sequence_cursor_is_ambiguous_without_touching_the_pipe", ClientCliRefusesWhenSequenceCursorIsAmbiguousWithoutTouchingThePipe),
    ("client_cli_refuses_when_sequence_cursor_does_not_match_verb_without_touching_the_pipe", ClientCliRefusesWhenSequenceCursorDoesNotMatchVerbWithoutTouchingThePipe),
    ("client_cli_fails_cleanly_when_sequence_cursor_content_is_corrupt", ClientCliFailsCleanlyWhenSequenceCursorContentIsCorrupt),
    ("client_cli_leaves_sequence_cursor_pending_when_response_is_lost", ClientCliLeavesSequenceCursorPendingWhenResponseIsLost),
    ("host_cli_startup_rolls_back_all_four_session_files_when_secret_save_fails", HostCliStartupRollsBackAllFourSessionFilesWhenSecretSaveFails),
    ("host_and_client_real_named_pipe_accepts_full_c0_through_c2_with_fake_launcher_and_sequence_cursor", HostAndClientRealNamedPipeAcceptsFullC0ThroughC2WithFakeLauncherAndSequenceCursor),

    // B4.4: real host process crash, real timeouts, real severed connections (Windows-only)
    ("real_host_process_crash_kills_root_via_kill_on_job_close", RealHostProcessCrashKillsRootViaKillOnJobClose),
    ("real_host_idle_timeout_before_any_connection_fails_closed_without_launching_root", RealHostIdleTimeoutBeforeAnyConnectionFailsClosedWithoutLaunchingRoot),
    ("real_host_idle_timeout_after_c0_tears_down_root_and_fails_closed", RealHostIdleTimeoutAfterC0TearsDownRootAndFailsClosed),
    ("real_submitter_timeout_during_c2_fails_closed_and_kills_submitter_and_root", RealSubmitterTimeoutDuringC2FailsClosedAndKillsSubmitterAndRoot),
    ("raw_malformed_request_before_any_mutation_leaves_session_fully_usable", RawMalformedRequestBeforeAnyMutationLeavesSessionFullyUsable),
    ("severed_connection_during_c1_does_not_allow_duplicate_acceptance_on_real_server", SeveredConnectionDuringC1DoesNotAllowDuplicateAcceptanceOnRealServer),
    ("severed_connection_during_c2_still_tears_down_root_and_submitter_on_real_server", SeveredConnectionDuringC2StillTearsDownRootAndSubmitterOnRealServer),
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
    var client = new C012ClientChannel(pair.ClientStream, secret, sessionId, 1);

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
    var client = new C012ClientChannel(pair.ClientStream, clientSecret, sessionId, 1);

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
    var client = new C012ClientChannel(firstPair.ClientStream, secret, sessionId, 1);

    Task<C012TransitionResult> firstClientTask =
        client.SendAsync(C012Control.C2, C012RequestType.SubmitConfig, ShortTestTimeout());
    _ = serverForFirst.ProcessNextRequestAsync(ShortTestTimeout()).GetAwaiter().GetResult();
    C012TransitionResult firstClientResult = firstClientTask.GetAwaiter().GetResult();
    Assert(!firstClientResult.Accepted, "premature C2 must be rejected");

    using DuplexPair secondPair = DuplexPair.Create();
    var serverForSecond = new C012ServerChannel(secondPair.ServerStream, secret, sequencer);
    var clientOnSecondPipe = new C012ClientChannel(secondPair.ClientStream, secret, sessionId, 1);
    // A fresh C012ClientChannel is given sequence 1 again; since the first (rejected) call
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

static void OrchestratingProcessorFailFromHostTearsDownAfterC0AndReachesFailedClosed()
{
    var launcher = new FakeRootProcessLauncher();
    Guid sessionId = Guid.NewGuid();
    var sequencer = new C012RequestSequencer(sessionId);
    var processor = new C012OrchestratingProcessor(sequencer, launcher);
    DriveC0ToRetained(processor, sessionId);

    // Simulates exactly what C012HostCli.ListenLoopAsync does on an idle timeout or an
    // unexpected loop error: call FailFromHost directly, never sequencer.ApplyInternal.
    processor.FailFromHost(C012Trigger.Timeout);

    Assert(sequencer.CurrentState == C012State.FailedClosed, "FailFromHost must fail the session closed");
    Assert(launcher.TeardownJobCallCount == 1, "FailFromHost must tear down the real Job created during C0");
}

static void OrchestratingProcessorFailFromHostIsIdempotentIfCalledTwice()
{
    var launcher = new FakeRootProcessLauncher();
    Guid sessionId = Guid.NewGuid();
    var sequencer = new C012RequestSequencer(sessionId);
    var processor = new C012OrchestratingProcessor(sequencer, launcher);
    DriveC0ToRetained(processor, sessionId);

    processor.FailFromHost(C012Trigger.Timeout);
    processor.FailFromHost(C012Trigger.OperationFailed);

    Assert(sequencer.CurrentState == C012State.FailedClosed, "the session must remain failed closed");
    Assert(launcher.TeardownJobCallCount == 1, "a second FailFromHost call must never tear down the Job twice");
}

static void OrchestratingProcessorFailFromHostRejectsClientOriginatedTriggers()
{
    var launcher = new FakeRootProcessLauncher();
    Guid sessionId = Guid.NewGuid();
    var sequencer = new C012RequestSequencer(sessionId);
    var processor = new C012OrchestratingProcessor(sequencer, launcher);

    bool threw = false;
    try
    {
        processor.FailFromHost(C012Trigger.BeginC0);
    }
    catch (ArgumentException)
    {
        threw = true;
    }

    Assert(threw, "FailFromHost must refuse a client-originated trigger, exactly like ApplyInternal does");
    Assert(launcher.Calls.Count == 0, "a refused FailFromHost call must never touch the launcher");
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

// ---- B4.2: session paths ----

static void SessionPathsDerivePipeNameIsDeterministicAndUniquePerSession()
{
    Guid sessionId = Guid.NewGuid();
    string first = C012SessionPaths.DerivePipeName(sessionId);
    string second = C012SessionPaths.DerivePipeName(sessionId);
    Assert(first == second, "the same session id must always derive the same pipe name");

    string other = C012SessionPaths.DerivePipeName(Guid.NewGuid());
    Assert(first != other, "different session ids must derive different pipe names");
}

static void SessionPathsWriteAndReadSessionIdRoundTrips()
{
    string directory = CreateTemporaryDirectory();
    try
    {
        Guid sessionId = Guid.NewGuid();
        C012SessionPaths.WriteSessionId(directory, sessionId);
        Guid read = C012SessionPaths.ReadSessionId(directory);
        Assert(read == sessionId, "the read session id must match what was written");
    }
    finally
    {
        Directory.Delete(directory, recursive: true);
    }
}

static void SessionPathsWriteSessionIdRefusesToOverwrite()
{
    string directory = CreateTemporaryDirectory();
    try
    {
        C012SessionPaths.WriteSessionId(directory, Guid.NewGuid());
        bool threw = false;
        try
        {
            C012SessionPaths.WriteSessionId(directory, Guid.NewGuid());
        }
        catch (IOException)
        {
            threw = true;
        }

        Assert(threw, "writing session.id a second time must be refused");
    }
    finally
    {
        Directory.Delete(directory, recursive: true);
    }
}

static void SessionPathsInspectReportsMissingDirectory()
{
    string directory = Path.Combine(Path.GetTempPath(), $"c012-missing-{Guid.NewGuid():N}");
    C012SessionDirStatus status = C012SessionPaths.Inspect(directory);
    Assert(!status.DirectoryExists, "a missing directory must report DirectoryExists=false");
    Assert(!status.SessionIdFilePresent && !status.SessionSecretFilePresent, "no files can be present in a missing directory");
    Assert(status.SessionId is null, "no session id can be read from a missing directory");
}

static void SessionPathsInspectReportsEmptyDirectory()
{
    string directory = CreateTemporaryDirectory();
    try
    {
        C012SessionDirStatus status = C012SessionPaths.Inspect(directory);
        Assert(status.DirectoryExists, "an existing empty directory must report DirectoryExists=true");
        Assert(!status.SessionIdFilePresent && !status.SessionSecretFilePresent, "an empty directory has no session files");
        Assert(status.SessionId is null, "no session id in an empty directory");
    }
    finally
    {
        Directory.Delete(directory, recursive: true);
    }
}

static void SessionPathsInspectReportsFullSession()
{
    string directory = CreateTemporaryDirectory();
    try
    {
        Guid sessionId = Guid.NewGuid();
        C012SessionPaths.WriteSessionId(directory, sessionId);
        using (C012SessionSecret secret = C012SessionSecret.Generate())
        {
            secret.Save(C012SessionPaths.SessionSecretPath(directory));
        }

        C012SessionDirStatus status = C012SessionPaths.Inspect(directory);
        Assert(
            status.DirectoryExists && status.SessionIdFilePresent && status.SessionSecretFilePresent,
            "a fully-started session must report both files present");
        Assert(status.SessionId == sessionId, "the reported session id must match what was written");
    }
    finally
    {
        Directory.Delete(directory, recursive: true);
    }
}

// ---- B4.2: placeholder launcher ----

static void NotImplementedLauncherCausesC0ToFailClosedViaOperationFailed()
{
    var launcher = new C012NotImplementedRootProcessLauncher();
    Guid sessionId = Guid.NewGuid();
    var sequencer = new C012RequestSequencer(sessionId);
    var processor = new C012OrchestratingProcessor(sequencer, launcher);

    C012TransitionResult result = processor.Apply(
        new C012RequestEnvelope(sessionId, 1, C012Control.C0, C012RequestType.Start));

    Assert(!result.Accepted, "C0 through the placeholder launcher must never be accepted");
    Assert(result.ResultingState == C012State.FailedClosed, "C0 through the placeholder launcher must fail closed");
    Assert(result.Reason == C012RejectionReason.OperationFailed, "the reason must be OperationFailed");
}

// ---- B4.2: client CLI ----

static void ClientCliStatusReportsMissingDirectoryWithoutClaimingLiveness()
{
    string directory = Path.Combine(Path.GetTempPath(), $"c012-status-missing-{Guid.NewGuid():N}");
    using var stdout = new StringWriter();
    using var stderr = new StringWriter();
    int exitCode = C012ClientCli.Run("status", ["--session-dir", directory], stdout, stderr);

    string report = stdout.ToString();
    Assert(report.Contains("session_dir_valid=False", StringComparison.Ordinal), "must report the directory is not valid");
    Assert(!report.Contains("alive", StringComparison.OrdinalIgnoreCase), "status must never claim liveness");
    Assert(!report.Contains("listening", StringComparison.OrdinalIgnoreCase), "status must never claim the host is listening");
    Assert(exitCode == C012ClientCli.ExitRejected, "a missing session must be reported as not-fully-present via the exit code");
}

static void ClientCliStatusReportsFullSessionWithoutClaimingLiveness()
{
    string directory = CreateTemporaryDirectory();
    try
    {
        Guid sessionId = Guid.NewGuid();
        C012SessionPaths.WriteSessionId(directory, sessionId);
        using (C012SessionSecret secret = C012SessionSecret.Generate())
        {
            secret.Save(C012SessionPaths.SessionSecretPath(directory));
        }

        C012SessionSequenceCursor.InitializeAtSessionStart(directory);

        using var stdout = new StringWriter();
        using var stderr = new StringWriter();
        int exitCode = C012ClientCli.Run("status", ["--session-dir", directory], stdout, stderr);

        string report = stdout.ToString();
        Assert(report.Contains($"session_id={sessionId:D}", StringComparison.Ordinal), "must report the actual session id");
        Assert(report.Contains("session_id_file_present=True", StringComparison.Ordinal), "must report session.id presence");
        Assert(report.Contains("session_secret_file_present=True", StringComparison.Ordinal), "must report session.secret presence");
        Assert(report.Contains("session_sequence_file_present=True", StringComparison.Ordinal), "must report session.sequence presence");
        Assert(report.Contains("session_sequence_lock_file_present=True", StringComparison.Ordinal), "must report session.sequence.lock presence");
        Assert(report.Contains("session_sequence_next=1 session_sequence_pending=False", StringComparison.Ordinal), "must report the fresh cursor state");
        Assert(!report.Contains("alive", StringComparison.OrdinalIgnoreCase), "status must never claim liveness");
        Assert(!report.Contains("listening", StringComparison.OrdinalIgnoreCase), "status must never claim the host is listening");
        Assert(exitCode == C012ClientCli.ExitAccepted, "a fully-present session reports exit code 0 from status");
    }
    finally
    {
        Directory.Delete(directory, recursive: true);
    }
}

static void ClientCliRequiresSessionDirFlag()
{
    using var stdout = new StringWriter();
    using var stderr = new StringWriter();
    int exitCode = C012ClientCli.Run("status", [], stdout, stderr);
    Assert(exitCode == C012ClientCli.ExitTransportFailure, "a missing --session-dir must be a transport-level failure");
    Assert(stderr.ToString().Contains("Usage", StringComparison.Ordinal), "must print a usage message");
}

static void ClientCliRejectsUnknownVerb()
{
    string directory = CreateTemporaryDirectory();
    try
    {
        using var stdout = new StringWriter();
        using var stderr = new StringWriter();
        int exitCode = C012ClientCli.Run("not-a-real-verb", ["--session-dir", directory], stdout, stderr);
        Assert(exitCode == C012ClientCli.ExitTransportFailure, "an unknown verb must be a transport-level failure");
    }
    finally
    {
        Directory.Delete(directory, recursive: true);
    }
}

// ---- B4.2: host CLI ----

static void HostCliRequiresSessionDirFlag()
{
    using var stdout = new StringWriter();
    using var stderr = new StringWriter();
    int exitCode = C012HostCli.Run([], stdout, stderr);
    Assert(exitCode == C012HostCli.ExitStartupFailure, "a missing --session-dir must be a startup failure");
    Assert(stderr.ToString().Contains("Usage", StringComparison.Ordinal), "must print a usage message");
}

static void HostCliRefusesWhenSessionDirMissing()
{
    string directory = Path.Combine(Path.GetTempPath(), $"c012-host-missing-{Guid.NewGuid():N}");
    using var stdout = new StringWriter();
    using var stderr = new StringWriter();
    int exitCode = C012HostCli.Run(["--session-dir", directory], stdout, stderr);
    Assert(exitCode == C012HostCli.ExitStartupFailure, "a non-existent --session-dir must be a startup failure");
}

static void HostCliRefusesWhenSessionAlreadyExists()
{
    string directory = CreateTemporaryDirectory();
    try
    {
        C012SessionPaths.WriteSessionId(directory, Guid.NewGuid());

        using var stdout = new StringWriter();
        using var stderr = new StringWriter();
        int exitCode = C012HostCli.Run(["--session-dir", directory], stdout, stderr);

        Assert(exitCode == C012HostCli.ExitStartupFailure, "an already-populated --session-dir must be refused");
        Assert(
            !File.Exists(C012SessionPaths.SessionSecretPath(directory)),
            "no secret may be written when refusing to reuse a session");
    }
    finally
    {
        Directory.Delete(directory, recursive: true);
    }
}

// ---- c012-host start-innocuous: narrow, explicitly authorized exception (see
// C012HostCli.RunInnocuous and lab/mt5_direct_endpoint/AGENTS.md). These two are
// OS-independent: both are rejected during argument/target validation, before the
// Windows-only pipe/session work ever begins. ----

static void HostCliStartInnocuousRequiresTargetFlag()
{
    string directory = CreateTemporaryDirectory();
    try
    {
        using var stdout = new StringWriter();
        using var stderr = new StringWriter();
        int exitCode = C012HostCli.RunInnocuous(["--session-dir", directory], stdout, stderr);

        Assert(exitCode == C012HostCli.ExitStartupFailure, "a missing --target must be a startup failure");
        Assert(stderr.ToString().Contains("Usage", StringComparison.Ordinal), "must print a usage message");
        Assert(!File.Exists(C012SessionPaths.SessionIdPath(directory)), "no session may start without a --target");
    }
    finally
    {
        Directory.Delete(directory, recursive: true);
    }
}

static void HostCliStartInnocuousRejectsUnknownTarget()
{
    string directory = CreateTemporaryDirectory();
    try
    {
        using var stdout = new StringWriter();
        using var stderr = new StringWriter();
        int exitCode = C012HostCli.RunInnocuous(
            ["--session-dir", directory, "--target", "not-an-allowlisted-target"], stdout, stderr);

        Assert(exitCode == C012HostCli.ExitStartupFailure, "an unknown --target must be a startup failure");
        Assert(stderr.ToString().Contains("Unknown --target", StringComparison.Ordinal), "must explain why the target was refused");
        Assert(!File.Exists(C012SessionPaths.SessionIdPath(directory)), "no session may start for an unknown target");
    }
    finally
    {
        Directory.Delete(directory, recursive: true);
    }
}

// ---- B4.2: real Named Pipe (Windows-only; each test returns immediately elsewhere) ----

static void HostAndClientRealNamedPipeRoundTripReachesFailedClosedViaPlaceholderLauncher()
{
    if (!OperatingSystem.IsWindows())
    {
        return;
    }

    string directory = CreateTemporaryDirectory();
    try
    {
        using var hostOut = new StringWriter();
        using var hostErr = new StringWriter();
        Task<int> hostTask = Task.Run(() => C012HostCli.Run(["--session-dir", directory], hostOut, hostErr));

        (int clientExitCode, string report) = RunClientWithRetries("c0-start", directory);

        Assert(
            clientExitCode == C012ClientCli.ExitRejected,
            "c0-start through the placeholder launcher must be rejected (not a transport failure)");
        Assert(report.Contains("accepted=False", StringComparison.Ordinal), "the request must not be accepted");
        Assert(report.Contains("resulting_state=FailedClosed", StringComparison.Ordinal), "the session must fail closed");
        Assert(report.Contains("reason=OperationFailed", StringComparison.Ordinal), "the reason must be OperationFailed");

        int hostExitCode = hostTask.GetAwaiter().GetResult();
        Assert(hostExitCode == C012HostCli.ExitFailedClosed, "the host must exit with the FailedClosed code");
        Assert(
            !File.Exists(C012SessionPaths.SessionSecretPath(directory)),
            "the secret file must be deleted once the session ends");
    }
    finally
    {
        Directory.Delete(directory, recursive: true);
    }
}

static void ClientCliFailsCleanlyWhenNoPipeIsListening()
{
    if (!OperatingSystem.IsWindows())
    {
        return;
    }

    string directory = CreateTemporaryDirectory();
    try
    {
        // A fully-populated session directory with no pipe ever created for it stands in
        // for "there is no live host to talk to" -- whether it crashed, was killed, or
        // never started, the client-observable behavior is the same: no pipe instance for
        // the OS to hand a connection to.
        Guid sessionId = Guid.NewGuid();
        C012SessionPaths.WriteSessionId(directory, sessionId);
        using (C012SessionSecret secret = C012SessionSecret.Generate())
        {
            secret.Save(C012SessionPaths.SessionSecretPath(directory));
        }

        C012SessionSequenceCursor.InitializeAtSessionStart(directory);

        using var stdout = new StringWriter();
        using var stderr = new StringWriter();
        int exitCode = C012ClientCli.Run("c0-start", ["--session-dir", directory], stdout, stderr);

        Assert(exitCode == C012ClientCli.ExitTransportFailure, "connecting when no pipe is listening must be a transport failure");
        C012SessionSequenceState? state = C012SessionSequenceCursor.TryReadSnapshot(directory);
        Assert(state is { Pending: false, SequenceNumber: 1 }, "a connect-time failure must never touch the sequence cursor");
    }
    finally
    {
        Directory.Delete(directory, recursive: true);
    }
}

// ---- B4.3: real Windows launcher (Windows-only; each test returns immediately elsewhere) ----

static void InnocuousLauncherC0ThroughC2ReachesTerminatedAndRootIsGoneAfterTeardown()
{
    if (!OperatingSystem.IsWindows())
    {
        return;
    }

    string executable = RequireCurrentExecutable();
    var launcher = new C012InnocuousRootProcessLauncher(
        executable,
        ComputeSha256(executable),
        SelfInvocationArguments(executable, "--innocent-sleeper"),
        SelfInvocationArguments(executable, "--innocent-exit-zero"),
        TimeSpan.FromSeconds(10));
    Guid sessionId = Guid.NewGuid();
    var sequencer = new C012RequestSequencer(sessionId);
    var processor = new C012OrchestratingProcessor(sequencer, launcher);

    C012TransitionResult c0 = processor.Apply(
        new C012RequestEnvelope(sessionId, 1, C012Control.C0, C012RequestType.Start));
    Assert(c0.Accepted && c0.ResultingState == C012State.C0Retained, "C0 must reach C0Retained with the real launcher");
    Assert(launcher.RootProcessId is not null, "the real root PID must be known after C0");

    uint rootPid = launcher.RootProcessId!.Value;
    long startTimeAfterC0;
    using (Process rootAfterC0 = Process.GetProcessById((int)rootPid))
    {
        startTimeAfterC0 = rootAfterC0.StartTime.ToUniversalTime().Ticks;
    }

    C012TransitionResult c1 = processor.Apply(
        new C012RequestEnvelope(sessionId, 2, C012Control.C1, C012RequestType.Query));
    Assert(c1.Accepted && c1.ResultingState == C012State.C1Retained, "C1 must reach C1Retained: same root, same generation");

    using (Process rootAfterC1 = Process.GetProcessById((int)rootPid))
    {
        Assert(
            rootAfterC1.StartTime.ToUniversalTime().Ticks == startTimeAfterC0,
            "the root observed at C1 must be the exact same process generation observed at C0");
    }

    C012TransitionResult c2 = processor.Apply(
        new C012RequestEnvelope(sessionId, 3, C012Control.C2, C012RequestType.SubmitConfig));
    Assert(
        c2.Accepted && c2.ResultingState == C012State.Terminated,
        "C2 must reach Terminated: the real submitter ran in the same real Job and real teardown succeeded");

    Assert(
        WaitUntilProcessIdIsGone(rootPid, TimeSpan.FromSeconds(5)),
        "the sleeper root (30s runtime) must already be gone immediately after C2 teardown, proving KILL_ON_JOB_CLOSE fired");
}

static void InnocuousLauncherEndToEndOverRealNamedPipeReachesTerminated()
{
    if (!OperatingSystem.IsWindows())
    {
        return;
    }

    string executable = RequireCurrentExecutable();
    var launcher = new C012InnocuousRootProcessLauncher(
        executable,
        ComputeSha256(executable),
        SelfInvocationArguments(executable, "--innocent-sleeper"),
        SelfInvocationArguments(executable, "--innocent-exit-zero"),
        TimeSpan.FromSeconds(10));

    string directory = CreateTemporaryDirectory();
    try
    {
        using var hostOut = new StringWriter();
        using var hostErr = new StringWriter();
        Task<int> hostTask = Task.Run(() => C012HostCli.Run(["--session-dir", directory], hostOut, hostErr, launcher));

        (int c0ExitCode, string c0Report) = RunClientWithRetries("c0-start", directory);
        Assert(c0ExitCode == C012ClientCli.ExitAccepted, "c0-start over the real pipe with the real launcher must be accepted");
        Assert(c0Report.Contains("resulting_state=C0Retained", StringComparison.Ordinal), "c0-start must reach C0Retained");

        (int c1ExitCode, string c1Report) = RunClientWithRetries("c1-query", directory);
        Assert(c1ExitCode == C012ClientCli.ExitAccepted, "c1-query over the real pipe must be accepted");
        Assert(c1Report.Contains("resulting_state=C1Retained", StringComparison.Ordinal), "c1-query must reach C1Retained");

        (int c2ExitCode, string c2Report) = RunClientWithRetries("c2-submit", directory);
        Assert(c2ExitCode == C012ClientCli.ExitAccepted, "c2-submit over the real pipe must be accepted");
        Assert(c2Report.Contains("resulting_state=Terminated", StringComparison.Ordinal), "c2-submit must reach Terminated");

        int hostExitCode = hostTask.GetAwaiter().GetResult();
        Assert(hostExitCode == C012HostCli.ExitTerminated, "the host must exit with the Terminated code");
        Assert(
            !File.Exists(C012SessionPaths.SessionSecretPath(directory)),
            "the secret file must be deleted once the session ends");
    }
    finally
    {
        Directory.Delete(directory, recursive: true);
    }
}

static void InnocuousLauncherClosingLastJobHandleKillsRootViaKillOnJobClose()
{
    if (!OperatingSystem.IsWindows())
    {
        return;
    }

    string executable = RequireCurrentExecutable();
    var launcher = new C012InnocuousRootProcessLauncher(
        executable,
        ComputeSha256(executable),
        SelfInvocationArguments(executable, "--innocent-sleeper"),
        SelfInvocationArguments(executable, "--innocent-exit-zero"),
        TimeSpan.FromSeconds(10));

    C012JobToken job = launcher.CreateJob();
    C012ProcessToken root = launcher.LaunchSuspendedRoot(job);
    launcher.AssignRootToJob(job, root);
    launcher.ResumeRoot(root);

    uint rootPid = launcher.RootProcessId!.Value;
    Assert(IsProcessRunning(rootPid), "the root must be running before the Job handle is closed");

    // Simulates "the last handle to this Job closed" -- identical at the OS level whether
    // that happens via this explicit call or because the owning host process was killed --
    // without needing to spawn and kill a second real process.
    C012InnocuousRootProcessLauncher.CloseJobHandleForTesting(job);

    Assert(
        WaitUntilProcessIdIsGone(rootPid, TimeSpan.FromSeconds(5)),
        "closing the last Job handle must kill the root process via KILL_ON_JOB_CLOSE");
}

static void InnocuousLauncherConstructorRefusesMetaTraderLikeExecutableName()
{
    if (!OperatingSystem.IsWindows())
    {
        return;
    }

    string directory = CreateTemporaryDirectory();
    try
    {
        string target = Path.Combine(directory, "terminal64.exe");
        File.Copy(RequireCurrentExecutable(), target);

        bool refused = false;
        try
        {
            _ = new C012InnocuousRootProcessLauncher(
                target,
                ComputeSha256(target),
                ["--innocent-exit-zero"],
                ["--innocent-exit-zero"],
                TimeSpan.FromSeconds(10));
        }
        catch (InvalidOperationException)
        {
            refused = true;
        }

        Assert(refused, "the constructor must refuse an executable named terminal64.exe regardless of its actual contents/hash");
    }
    finally
    {
        Directory.Delete(directory, recursive: true);
    }
}

// The only test exercising the actual `start-innocuous` verb end to end (real named pipe,
// real C012InnocuousRootProcessLauncher, real Job Object) rather than constructing the
// launcher directly. Defense-in-depth against an MT5-named target is already proven
// independently by InnocuousLauncherConstructorRefusesMetaTraderLikeExecutableName above --
// not re-proven here, since RunInnocuous's allowlist can never select an MT5-named
// executable in the first place (its one allowed target is this very process re-invoking
// itself, never anything caller-supplied).
static void HostCliStartInnocuousWithSelfSleeperTargetReachesTerminatedOverRealNamedPipe()
{
    if (!OperatingSystem.IsWindows())
    {
        return;
    }

    // Must spawn the real, separate JobHarness.exe/.dll process rather than call
    // C012HostCli.RunInnocuous in-process here: RunInnocuous's self-invocation resolves
    // against Environment.ProcessPath of whatever process calls it, and this test project's
    // own Program.cs does not understand --innocent-lab-sleeper/--innocent-lab-exit-zero (it
    // has its own, differently-named self-invocation flags) -- calling it in-process would
    // silently re-run this entire test suite as the "root"/"submitter" instead of a harmless
    // sleeper/immediate-exit, and was exactly the bug a first version of this test had.
    string directory = CreateTemporaryDirectory();
    Process? hostProcess = null;
    try
    {
        hostProcess = StartInnocuousHostProcess(directory);

        (int c0ExitCode, string c0Report) = RunClientWithRetries("c0-start", directory);
        Assert(c0ExitCode == C012ClientCli.ExitAccepted, "c0-start against a real start-innocuous host must be accepted");
        Assert(c0Report.Contains("resulting_state=C0Retained", StringComparison.Ordinal), "c0-start must reach C0Retained");

        (int c1ExitCode, string c1Report) = RunClientWithRetries("c1-query", directory);
        Assert(c1ExitCode == C012ClientCli.ExitAccepted, "c1-query must be accepted");
        Assert(c1Report.Contains("resulting_state=C1Retained", StringComparison.Ordinal), "c1-query must reach C1Retained");

        (int c2ExitCode, string c2Report) = RunClientWithRetries("c2-submit", directory);
        Assert(c2ExitCode == C012ClientCli.ExitAccepted, "c2-submit must be accepted");
        Assert(c2Report.Contains("resulting_state=Terminated", StringComparison.Ordinal), "c2-submit must reach Terminated");

        Assert(hostProcess.WaitForExit(TimeSpan.FromSeconds(10)), "the start-innocuous host process must exit on its own after Terminated");
        Assert(hostProcess.ExitCode == C012HostCli.ExitTerminated, "the start-innocuous host must exit with the Terminated code");

        AssertSessionFullyTornDown(directory, expectSecretDeleted: true);
    }
    finally
    {
        if (hostProcess is { HasExited: false })
        {
            hostProcess.Kill(entireProcessTree: false);
        }

        hostProcess?.Dispose();
        Directory.Delete(directory, recursive: true);
    }
}

static string FindJobHarnessDll()
{
    DirectoryInfo? directory = new DirectoryInfo(AppContext.BaseDirectory);
    while (directory is not null && directory.Name != "JobHarness")
    {
        directory = directory.Parent;
    }

    if (directory is null)
    {
        throw new InvalidOperationException("Could not locate the JobHarness project directory from the test's own output path.");
    }

    string binDirectory = Path.Combine(directory.FullName, "bin");
    string[] candidates = Directory.Exists(binDirectory)
        ? Directory.GetFiles(binDirectory, "JobHarness.dll", SearchOption.AllDirectories)
        : [];

    if (candidates.Length == 0)
    {
        throw new InvalidOperationException($"JobHarness.dll has not been built yet under '{binDirectory}'.");
    }

    return candidates.OrderByDescending(File.GetLastWriteTimeUtc).First();
}

static Process StartInnocuousHostProcess(string sessionDir)
{
    var startInfo = new ProcessStartInfo
    {
        FileName = "dotnet",
        UseShellExecute = false,
        CreateNoWindow = true,
    };
    startInfo.ArgumentList.Add(FindJobHarnessDll());
    startInfo.ArgumentList.Add("c012-host");
    startInfo.ArgumentList.Add("start-innocuous");
    startInfo.ArgumentList.Add("--session-dir");
    startInfo.ArgumentList.Add(sessionDir);
    startInfo.ArgumentList.Add("--target");
    startInfo.ArgumentList.Add(C012HostCli.InnocuousTargetSelfSleeper);

    return Process.Start(startInfo) ?? throw new InvalidOperationException("Unable to start the start-innocuous host process for testing.");
}

// ---- B4.2 fix: session sequence cursor continuity (Windows-only; each test returns
// immediately elsewhere) ----

static void SessionSequenceCursorAdvancesOneTwoThreeOnAcceptedResponses()
{
    if (!OperatingSystem.IsWindows())
    {
        return;
    }

    string directory = CreateTemporaryDirectory();
    try
    {
        C012SessionSequenceCursor.InitializeAtSessionStart(directory);

        foreach (long expected in new long[] { 1, 2, 3 })
        {
            using C012SessionSequenceCursor cursor = C012SessionSequenceCursor.AcquireExclusive(directory);
            C012SessionSequenceState state = cursor.Read();
            Assert(!state.Pending, $"cursor must be clean before sequence {expected}");
            Assert(state.SequenceNumber == expected, $"cursor must show {expected} before that step");
            cursor.WritePending(state.SequenceNumber);
            cursor.WriteClean(state.SequenceNumber + 1);
        }

        C012SessionSequenceState? finalState = C012SessionSequenceCursor.TryReadSnapshot(directory);
        Assert(finalState is { Pending: false, SequenceNumber: 4 }, "after three accepted steps the cursor must show 4");
    }
    finally
    {
        Directory.Delete(directory, recursive: true);
    }
}

static void SessionSequenceCursorConcurrentAcquireIsRejectedAsBusy()
{
    if (!OperatingSystem.IsWindows())
    {
        return;
    }

    string directory = CreateTemporaryDirectory();
    try
    {
        C012SessionSequenceCursor.InitializeAtSessionStart(directory);
        using C012SessionSequenceCursor first = C012SessionSequenceCursor.AcquireExclusive(directory);

        bool threw = false;
        try
        {
            using C012SessionSequenceCursor second = C012SessionSequenceCursor.AcquireExclusive(directory);
        }
        catch (C012SessionCursorBusyException)
        {
            threw = true;
        }

        Assert(threw, "a second concurrent acquire must be rejected while the first is still held");
    }
    finally
    {
        Directory.Delete(directory, recursive: true);
    }
}

static void SessionSequenceCursorWriteAheadPendingSurvivesReacquire()
{
    if (!OperatingSystem.IsWindows())
    {
        return;
    }

    string directory = CreateTemporaryDirectory();
    try
    {
        C012SessionSequenceCursor.InitializeAtSessionStart(directory);
        using (C012SessionSequenceCursor cursor = C012SessionSequenceCursor.AcquireExclusive(directory))
        {
            cursor.WritePending(1);
        }

        // Simulates a crash right after the write-ahead marker was durably written: the lock
        // is released (as the OS would release it on process exit) but the marker remains.
        using (C012SessionSequenceCursor reacquired = C012SessionSequenceCursor.AcquireExclusive(directory))
        {
            C012SessionSequenceState state = reacquired.Read();
            Assert(state is { Pending: true, SequenceNumber: 1 }, "the pending marker must survive across a lock release");
        }
    }
    finally
    {
        Directory.Delete(directory, recursive: true);
    }
}

static void SessionSequenceCursorCleanRejectionLeavesSameSequenceReusable()
{
    if (!OperatingSystem.IsWindows())
    {
        return;
    }

    string directory = CreateTemporaryDirectory();
    try
    {
        C012SessionSequenceCursor.InitializeAtSessionStart(directory);
        using (C012SessionSequenceCursor cursor = C012SessionSequenceCursor.AcquireExclusive(directory))
        {
            cursor.WritePending(1);
            // Simulates a definite, unambiguous Rejected response: the same sequence number
            // remains legal to retry, unlike the ambiguous case above.
            cursor.WriteClean(1);
        }

        C012SessionSequenceState? state = C012SessionSequenceCursor.TryReadSnapshot(directory);
        Assert(state is { Pending: false, SequenceNumber: 1 }, "a clean rejection must leave the same sequence number reusable");
    }
    finally
    {
        Directory.Delete(directory, recursive: true);
    }
}

static void ClientCliRefusesWhenSequenceCursorIsAmbiguousWithoutTouchingThePipe()
{
    if (!OperatingSystem.IsWindows())
    {
        return;
    }

    string directory = CreateTemporaryDirectory();
    try
    {
        Guid sessionId = Guid.NewGuid();
        C012SessionPaths.WriteSessionId(directory, sessionId);
        using (C012SessionSecret secret = C012SessionSecret.Generate())
        {
            secret.Save(C012SessionPaths.SessionSecretPath(directory));
        }

        C012SessionSequenceCursor.InitializeAtSessionStart(directory);
        using (C012SessionSequenceCursor cursor = C012SessionSequenceCursor.AcquireExclusive(directory))
        {
            cursor.WritePending(1);
        }

        // No pipe is ever created for this session: if the client tried to connect, it would
        // hang until ConnectTimeoutSeconds and report a timeout, not an "ambiguous" message.
        using var stdout = new StringWriter();
        using var stderr = new StringWriter();
        int exitCode = C012ClientCli.Run("c0-start", ["--session-dir", directory], stdout, stderr);

        Assert(exitCode == C012ClientCli.ExitTransportFailure, "an ambiguous cursor must be a transport-level failure");
        Assert(stderr.ToString().Contains("ambiguous", StringComparison.OrdinalIgnoreCase), "the refusal must clearly say the cursor is ambiguous");
        Assert(!stderr.ToString().Contains("Timed out", StringComparison.Ordinal), "the refusal must happen before any connection attempt");
    }
    finally
    {
        Directory.Delete(directory, recursive: true);
    }
}

static void ClientCliRefusesWhenSequenceCursorDoesNotMatchVerbWithoutTouchingThePipe()
{
    if (!OperatingSystem.IsWindows())
    {
        return;
    }

    string directory = CreateTemporaryDirectory();
    try
    {
        Guid sessionId = Guid.NewGuid();
        C012SessionPaths.WriteSessionId(directory, sessionId);
        using (C012SessionSecret secret = C012SessionSecret.Generate())
        {
            secret.Save(C012SessionPaths.SessionSecretPath(directory));
        }

        C012SessionSequenceCursor.InitializeAtSessionStart(directory);

        // The cursor is clean at sequence 1 (c0-start), but c1-query expects 2: no pipe is
        // ever created for this session, so a connection attempt would hang until timeout.
        using var stdout = new StringWriter();
        using var stderr = new StringWriter();
        int exitCode = C012ClientCli.Run("c1-query", ["--session-dir", directory], stdout, stderr);

        Assert(exitCode == C012ClientCli.ExitTransportFailure, "a sequence mismatch must be a transport-level failure");
        Assert(stderr.ToString().Contains("expects sequence 2", StringComparison.Ordinal), "the refusal must explain the mismatch");
        Assert(!stderr.ToString().Contains("Timed out", StringComparison.Ordinal), "the refusal must happen before any connection attempt");
    }
    finally
    {
        Directory.Delete(directory, recursive: true);
    }
}

static void ClientCliFailsCleanlyWhenSequenceCursorContentIsCorrupt()
{
    if (!OperatingSystem.IsWindows())
    {
        return;
    }

    string directory = CreateTemporaryDirectory();
    try
    {
        Guid sessionId = Guid.NewGuid();
        C012SessionPaths.WriteSessionId(directory, sessionId);
        using (C012SessionSecret secret = C012SessionSecret.Generate())
        {
            secret.Save(C012SessionPaths.SessionSecretPath(directory));
        }

        C012SessionSequenceCursor.InitializeAtSessionStart(directory);
        File.WriteAllText(C012SessionPaths.SessionSequencePath(directory), "not-a-number", System.Text.Encoding.UTF8);

        using var stdout = new StringWriter();
        using var stderr = new StringWriter();
        int exitCode = C012ClientCli.Run("c0-start", ["--session-dir", directory], stdout, stderr);

        Assert(exitCode == C012ClientCli.ExitTransportFailure, "a corrupt sequence cursor must fail closed, not crash or guess");
    }
    finally
    {
        Directory.Delete(directory, recursive: true);
    }
}

static void ClientCliLeavesSequenceCursorPendingWhenResponseIsLost()
{
    if (!OperatingSystem.IsWindows())
    {
        return;
    }

    string directory = CreateTemporaryDirectory();
    try
    {
        Guid sessionId = Guid.NewGuid();
        C012SessionPaths.WriteSessionId(directory, sessionId);
        using (C012SessionSecret secret = C012SessionSecret.Generate())
        {
            secret.Save(C012SessionPaths.SessionSecretPath(directory));
        }

        C012SessionSequenceCursor.InitializeAtSessionStart(directory);

        string pipeName = C012SessionPaths.DerivePipeName(sessionId);
        using var serverPipe = new NamedPipeServerStream(
            pipeName, PipeDirection.InOut, 1, PipeTransmissionMode.Byte, PipeOptions.Asynchronous);
        Task serverTask = Task.Run(async () =>
        {
            await serverPipe.WaitForConnectionAsync().ConfigureAwait(false);
            // Reads exactly one request frame -- so the request genuinely arrived -- then
            // disconnects without ever writing a response.
            _ = await C012FrameCodec.ReadFrameAsync(serverPipe, CancellationToken.None).ConfigureAwait(false);
            serverPipe.Disconnect();
        });

        using var stdout = new StringWriter();
        using var stderr = new StringWriter();
        int exitCode = C012ClientCli.Run("c0-start", ["--session-dir", directory], stdout, stderr);
        serverTask.GetAwaiter().GetResult();

        Assert(exitCode == C012ClientCli.ExitTransportFailure, "a lost response must be a transport-level failure");

        C012SessionSequenceState? state = C012SessionSequenceCursor.TryReadSnapshot(directory);
        Assert(state is { Pending: true, SequenceNumber: 1 }, "the cursor must be left pending at sequence 1 after a lost response");

        // A second attempt must refuse locally, without ever touching the pipe again (there
        // is no listener left for it to reach).
        using var secondStdout = new StringWriter();
        using var secondStderr = new StringWriter();
        int secondExitCode = C012ClientCli.Run("c0-start", ["--session-dir", directory], secondStdout, secondStderr);
        Assert(secondExitCode == C012ClientCli.ExitTransportFailure, "a subsequent attempt on an ambiguous cursor must also fail");
        Assert(secondStderr.ToString().Contains("ambiguous", StringComparison.OrdinalIgnoreCase), "the second refusal must also cite ambiguity");
    }
    finally
    {
        Directory.Delete(directory, recursive: true);
    }
}

static void HostCliStartupRollsBackAllFourSessionFilesWhenSecretSaveFails()
{
    if (!OperatingSystem.IsWindows())
    {
        return;
    }

    string directory = CreateTemporaryDirectory();
    try
    {
        // Occupies session.secret's path with a directory: File.Exists (used by the
        // pre-flight "already contains a session" check) reports false for a directory, so
        // the atomic startup block is entered -- but File.Move inside C012SessionSecret.Save
        // still fails deterministically once it tries to move a file onto that path.
        Directory.CreateDirectory(C012SessionPaths.SessionSecretPath(directory));

        using var stdout = new StringWriter();
        using var stderr = new StringWriter();
        int exitCode = C012HostCli.Run(["--session-dir", directory], stdout, stderr);

        Assert(exitCode == C012HostCli.ExitStartupFailure, "a failed atomic startup must be a startup failure");
        Assert(!File.Exists(C012SessionPaths.SessionIdPath(directory)), "session.id must be rolled back");
        Assert(!File.Exists(C012SessionPaths.SessionSequencePath(directory)), "session.sequence must be rolled back");
        Assert(!File.Exists(C012SessionPaths.SessionSequenceLockPath(directory)), "session.sequence.lock must be rolled back");
    }
    finally
    {
        Directory.Delete(directory, recursive: true);
    }
}

static void HostAndClientRealNamedPipeAcceptsFullC0ThroughC2WithFakeLauncherAndSequenceCursor()
{
    if (!OperatingSystem.IsWindows())
    {
        return;
    }

    string directory = CreateTemporaryDirectory();
    try
    {
        var launcher = new FakeRootProcessLauncher();
        using var hostOut = new StringWriter();
        using var hostErr = new StringWriter();
        Task<int> hostTask = Task.Run(() => C012HostCli.Run(["--session-dir", directory], hostOut, hostErr, launcher));

        (int c0ExitCode, string c0Report) = RunClientWithRetries("c0-start", directory);
        Assert(c0ExitCode == C012ClientCli.ExitAccepted, "c0-start must be accepted");
        Assert(c0Report.Contains("resulting_state=C0Retained", StringComparison.Ordinal), "c0-start must reach C0Retained");

        (int c1ExitCode, string c1Report) = RunClientWithRetries("c1-query", directory);
        Assert(c1ExitCode == C012ClientCli.ExitAccepted, "c1-query must be accepted");
        Assert(c1Report.Contains("resulting_state=C1Retained", StringComparison.Ordinal), "c1-query must reach C1Retained");

        (int c2ExitCode, string c2Report) = RunClientWithRetries("c2-submit", directory);
        Assert(c2ExitCode == C012ClientCli.ExitAccepted, "c2-submit must be accepted");
        Assert(c2Report.Contains("resulting_state=Terminated", StringComparison.Ordinal), "c2-submit must reach Terminated");

        int hostExitCode = hostTask.GetAwaiter().GetResult();
        Assert(hostExitCode == C012HostCli.ExitTerminated, "the host must exit with the Terminated code");

        C012SessionSequenceState? finalState = C012SessionSequenceCursor.TryReadSnapshot(directory);
        Assert(finalState is { Pending: false, SequenceNumber: 4 }, "after three accepted steps the cursor must show 4");
    }
    finally
    {
        Directory.Delete(directory, recursive: true);
    }
}

// ---- B4.4: real host process crash, real timeouts, real severed connections (Windows-only;
// each test returns immediately elsewhere) ----

static void RealHostProcessCrashKillsRootViaKillOnJobClose()
{
    if (!OperatingSystem.IsWindows())
    {
        return;
    }

    string directory = CreateTemporaryDirectory();
    string rootReadyPath = Path.Combine(directory, "root-ready.txt");
    Process? hostProcess = null;
    try
    {
        hostProcess = StartRealHostProcessForCrashTest(directory, rootReadyPath, idleTimeoutSeconds: 30);

        (int c0ExitCode, string c0Report) = RunClientWithRetries("c0-start", directory);
        Assert(c0ExitCode == C012ClientCli.ExitAccepted, "c0-start against the separate real host process must be accepted");
        Assert(c0Report.Contains("resulting_state=C0Retained", StringComparison.Ordinal), "c0-start must reach C0Retained");

        (int rootPid, _) = WaitForReadyRecord(rootReadyPath, TimeSpan.FromSeconds(10));
        Assert(IsProcessRunning((uint)rootPid), "the root must be running before the host process is killed");

        // entireProcessTree:false is required, not incidental: .NET's tree-kill would hunt
        // down and terminate the root process directly, which would make this test pass for
        // the wrong reason. The root must die *only* because the Job's last handle -- owned
        // solely by the host process -- closed when that process was terminated.
        hostProcess.Kill(entireProcessTree: false);
        Assert(hostProcess.WaitForExit(TimeSpan.FromSeconds(10)), "the killed host process must actually exit");

        Assert(
            WaitUntilProcessIdIsGone((uint)rootPid, TimeSpan.FromSeconds(10)),
            "the root must be gone after the host process was killed, via KILL_ON_JOB_CLOSE alone");

        // A hard kill gives the host's own finally block (which deletes session.secret) no
        // chance to run: unlike every other B4.4 scenario, this is not a graceful exit.
        AssertSessionFullyTornDown(directory, expectSecretDeleted: false);
        C012SessionSequenceState? finalState = C012SessionSequenceCursor.TryReadSnapshot(directory);
        Assert(finalState is { Pending: false, SequenceNumber: 2 }, "the cursor must reflect the one confirmed, accepted c0-start");
    }
    finally
    {
        if (hostProcess is { HasExited: false })
        {
            hostProcess.Kill(entireProcessTree: false);
        }

        hostProcess?.Dispose();
        Directory.Delete(directory, recursive: true);
    }
}

static void RealHostIdleTimeoutBeforeAnyConnectionFailsClosedWithoutLaunchingRoot()
{
    if (!OperatingSystem.IsWindows())
    {
        return;
    }

    string executable = RequireCurrentExecutable();
    var launcher = new C012InnocuousRootProcessLauncher(
        executable,
        ComputeSha256(executable),
        SelfInvocationArguments(executable, "--innocent-sleeper"),
        SelfInvocationArguments(executable, "--innocent-exit-zero"),
        TimeSpan.FromSeconds(10));

    string directory = CreateTemporaryDirectory();
    try
    {
        using var hostOut = new StringWriter();
        using var hostErr = new StringWriter();
        int hostExitCode = C012HostCli.Run(
            ["--session-dir", directory], hostOut, hostErr, launcher, TimeSpan.FromSeconds(3));

        Assert(hostExitCode == C012HostCli.ExitFailedClosed, "an idle timeout before any connection must fail closed");
        Assert(launcher.RootProcessId is null, "the root must never be launched if no connection ever arrives");

        AssertSessionFullyTornDown(directory, expectSecretDeleted: true);
    }
    finally
    {
        Directory.Delete(directory, recursive: true);
    }
}

static void RealHostIdleTimeoutAfterC0TearsDownRootAndFailsClosed()
{
    if (!OperatingSystem.IsWindows())
    {
        return;
    }

    string executable = RequireCurrentExecutable();
    string directory = CreateTemporaryDirectory();
    string rootReadyPath = Path.Combine(directory, "root-ready.txt");
    try
    {
        var launcher = new C012InnocuousRootProcessLauncher(
            executable,
            ComputeSha256(executable),
            SelfInvocationArguments(executable, "--innocent-ready-then-sleep", rootReadyPath),
            SelfInvocationArguments(executable, "--innocent-exit-zero"),
            TimeSpan.FromSeconds(10));

        using var hostOut = new StringWriter();
        using var hostErr = new StringWriter();
        Task<int> hostTask = Task.Run(() => C012HostCli.Run(
            ["--session-dir", directory], hostOut, hostErr, launcher, TimeSpan.FromSeconds(3)));

        (int c0ExitCode, string c0Report) = RunClientWithRetries("c0-start", directory);
        Assert(c0ExitCode == C012ClientCli.ExitAccepted, "c0-start must be accepted before the idle timeout is exercised");
        Assert(c0Report.Contains("resulting_state=C0Retained", StringComparison.Ordinal), "c0-start must reach C0Retained");

        (int rootPid, _) = WaitForReadyRecord(rootReadyPath, TimeSpan.FromSeconds(10));

        // No c1-query is ever attempted: the host's own idle timeout must fire while sitting
        // in C0Retained, waiting for a connection that never comes.
        int hostExitCode = hostTask.GetAwaiter().GetResult();
        Assert(hostExitCode == C012HostCli.ExitFailedClosed, "an idle timeout after C0 must fail closed");

        Assert(
            WaitUntilProcessIdIsGone((uint)rootPid, TimeSpan.FromSeconds(10)),
            "the root must be torn down once the session fails closed on idle timeout");

        AssertSessionFullyTornDown(directory, expectSecretDeleted: true);
    }
    finally
    {
        Directory.Delete(directory, recursive: true);
    }
}

static void RealSubmitterTimeoutDuringC2FailsClosedAndKillsSubmitterAndRoot()
{
    if (!OperatingSystem.IsWindows())
    {
        return;
    }

    string executable = RequireCurrentExecutable();
    string directory = CreateTemporaryDirectory();
    string rootReadyPath = Path.Combine(directory, "root-ready.txt");
    string submitterReadyPath = Path.Combine(directory, "submitter-ready.txt");
    try
    {
        var launcher = new C012InnocuousRootProcessLauncher(
            executable,
            ComputeSha256(executable),
            SelfInvocationArguments(executable, "--innocent-ready-then-sleep", rootReadyPath),
            // The submitter deliberately never exits on its own: this is what forces
            // ResumeAndAwaitSubmitter's own WaitForSingleObject timeout (already implemented
            // in B4.3, unmodified here) to fire for real.
            SelfInvocationArguments(executable, "--innocent-ready-then-sleep", submitterReadyPath),
            TimeSpan.FromSeconds(2));

        using var hostOut = new StringWriter();
        using var hostErr = new StringWriter();
        Task<int> hostTask = Task.Run(() => C012HostCli.Run(
            ["--session-dir", directory], hostOut, hostErr, launcher, TimeSpan.FromSeconds(30)));

        (int c0ExitCode, _) = RunClientWithRetries("c0-start", directory);
        Assert(c0ExitCode == C012ClientCli.ExitAccepted, "c0-start must be accepted");
        (int rootPid, _) = WaitForReadyRecord(rootReadyPath, TimeSpan.FromSeconds(10));

        (int c1ExitCode, _) = RunClientWithRetries("c1-query", directory);
        Assert(c1ExitCode == C012ClientCli.ExitAccepted, "c1-query must be accepted");

        (int c2ExitCode, string c2Report) = RunClientWithRetries("c2-submit", directory);
        Assert(c2ExitCode == C012ClientCli.ExitRejected, "c2-submit must be rejected once the submitter times out");
        Assert(
            c2Report.Contains("resulting_state=FailedClosed", StringComparison.Ordinal),
            "the session must fail closed on a real submitter timeout");

        (int submitterPid, _) = WaitForReadyRecord(submitterReadyPath, TimeSpan.FromSeconds(10));

        int hostExitCode = hostTask.GetAwaiter().GetResult();
        Assert(hostExitCode == C012HostCli.ExitFailedClosed, "the host must exit with the FailedClosed code");

        Assert(
            WaitUntilProcessIdIsGone((uint)submitterPid, TimeSpan.FromSeconds(10)),
            "the submitter must be killed by teardown after its own timeout");
        Assert(
            WaitUntilProcessIdIsGone((uint)rootPid, TimeSpan.FromSeconds(10)),
            "the root must also be killed: teardown closes the whole Job, not just the submitter");

        AssertSessionFullyTornDown(directory, expectSecretDeleted: true);
    }
    finally
    {
        Directory.Delete(directory, recursive: true);
    }
}

static void RawMalformedRequestBeforeAnyMutationLeavesSessionFullyUsable()
{
    if (!OperatingSystem.IsWindows())
    {
        return;
    }

    string executable = RequireCurrentExecutable();
    string directory = CreateTemporaryDirectory();
    try
    {
        var launcher = new C012InnocuousRootProcessLauncher(
            executable,
            ComputeSha256(executable),
            SelfInvocationArguments(executable, "--innocent-sleeper"),
            SelfInvocationArguments(executable, "--innocent-exit-zero"),
            TimeSpan.FromSeconds(10));

        using var hostOut = new StringWriter();
        using var hostErr = new StringWriter();
        Task<int> hostTask = Task.Run(() => C012HostCli.Run(
            ["--session-dir", directory], hostOut, hostErr, launcher, TimeSpan.FromSeconds(10)));

        string pipeName = WaitForPipeName(directory, TimeSpan.FromSeconds(10));
        SendRawMalformedFrameAndDisconnect(pipeName);

        (int c0ExitCode, string c0Report) = RunClientWithRetries("c0-start", directory);
        Assert(c0ExitCode == C012ClientCli.ExitAccepted, "a real c0-start after a raw malformed attempt must still be accepted normally");
        Assert(c0Report.Contains("resulting_state=C0Retained", StringComparison.Ordinal), "c0-start must reach C0Retained");

        (int c1ExitCode, _) = RunClientWithRetries("c1-query", directory);
        Assert(c1ExitCode == C012ClientCli.ExitAccepted, "c1-query must also proceed normally afterward");

        (int c2ExitCode, string c2Report) = RunClientWithRetries("c2-submit", directory);
        Assert(c2ExitCode == C012ClientCli.ExitAccepted, "c2-submit must also proceed normally afterward");
        Assert(c2Report.Contains("resulting_state=Terminated", StringComparison.Ordinal), "the session must terminate normally");

        int hostExitCode = hostTask.GetAwaiter().GetResult();
        Assert(hostExitCode == C012HostCli.ExitTerminated, "the host must exit with the Terminated code");

        AssertSessionFullyTornDown(directory, expectSecretDeleted: true);
    }
    finally
    {
        Directory.Delete(directory, recursive: true);
    }
}

static void SeveredConnectionDuringC1DoesNotAllowDuplicateAcceptanceOnRealServer()
{
    if (!OperatingSystem.IsWindows())
    {
        return;
    }

    string executable = RequireCurrentExecutable();
    string directory = CreateTemporaryDirectory();
    string rootReadyPath = Path.Combine(directory, "root-ready.txt");
    try
    {
        var launcher = new C012InnocuousRootProcessLauncher(
            executable,
            ComputeSha256(executable),
            SelfInvocationArguments(executable, "--innocent-ready-then-sleep", rootReadyPath),
            SelfInvocationArguments(executable, "--innocent-exit-zero"),
            TimeSpan.FromSeconds(10));

        using var hostOut = new StringWriter();
        using var hostErr = new StringWriter();
        Task<int> hostTask = Task.Run(() => C012HostCli.Run(
            ["--session-dir", directory], hostOut, hostErr, launcher, TimeSpan.FromSeconds(10)));

        (int c0ExitCode, _) = RunClientWithRetries("c0-start", directory);
        Assert(c0ExitCode == C012ClientCli.ExitAccepted, "c0-start must be accepted");
        (int rootPid, _) = WaitForReadyRecord(rootReadyPath, TimeSpan.FromSeconds(10));

        Guid sessionId = C012SessionPaths.Inspect(directory).SessionId!.Value;
        using (C012SessionSecret secret = C012SessionSecret.Load(C012SessionPaths.SessionSecretPath(directory)))
        {
            string pipeName = C012SessionPaths.DerivePipeName(sessionId);

            // A real, correctly-signed C1 request against the real, fully-functioning host --
            // severed immediately after the request is sent, before any response is read.
            SendRawSignedRequest(pipeName, secret, sessionId, 2, C012Control.C1, C012RequestType.Query, readResponse: false);

            // A duplicate at the same, already-consumed sequence number must be rejected:
            // proof that the real server's FSM genuinely advanced from the severed attempt
            // and was never re-executed.
            C012TransitionResult? duplicate = SendRawSignedRequest(
                pipeName, secret, sessionId, 2, C012Control.C1, C012RequestType.Query, readResponse: true);
            Assert(duplicate is { Accepted: false }, "a duplicate C1 at the already-consumed sequence must be rejected");

            // The correct next sequence number (C2) is still accepted: proof the real server
            // is in a sane, coherent state, not stuck or corrupted by the severed connection.
            C012TransitionResult? next = SendRawSignedRequest(
                pipeName, secret, sessionId, 3, C012Control.C2, C012RequestType.SubmitConfig, readResponse: true);
            Assert(
                next is { Accepted: true, ResultingState: C012State.Terminated },
                "the real server must still accept the correct next request after the severed connection");
        }

        int hostExitCode = hostTask.GetAwaiter().GetResult();
        Assert(
            hostExitCode == C012HostCli.ExitTerminated,
            "the host must reach Terminated: C1 (via the severed-but-processed request) and C2 both genuinely succeeded server-side");

        Assert(WaitUntilProcessIdIsGone((uint)rootPid, TimeSpan.FromSeconds(10)), "the root must be gone after the real teardown");

        AssertSessionFullyTornDown(directory, expectSecretDeleted: true);
    }
    finally
    {
        Directory.Delete(directory, recursive: true);
    }
}

static void SeveredConnectionDuringC2StillTearsDownRootAndSubmitterOnRealServer()
{
    if (!OperatingSystem.IsWindows())
    {
        return;
    }

    string executable = RequireCurrentExecutable();
    string directory = CreateTemporaryDirectory();
    string rootReadyPath = Path.Combine(directory, "root-ready.txt");
    string submitterReadyPath = Path.Combine(directory, "submitter-ready.txt");
    try
    {
        var launcher = new C012InnocuousRootProcessLauncher(
            executable,
            ComputeSha256(executable),
            SelfInvocationArguments(executable, "--innocent-ready-then-sleep", rootReadyPath),
            SelfInvocationArguments(executable, "--innocent-ready-then-exit", submitterReadyPath),
            TimeSpan.FromSeconds(10));

        using var hostOut = new StringWriter();
        using var hostErr = new StringWriter();
        Task<int> hostTask = Task.Run(() => C012HostCli.Run(
            ["--session-dir", directory], hostOut, hostErr, launcher, TimeSpan.FromSeconds(15)));

        (int c0ExitCode, _) = RunClientWithRetries("c0-start", directory);
        Assert(c0ExitCode == C012ClientCli.ExitAccepted, "c0-start must be accepted");
        (int rootPid, _) = WaitForReadyRecord(rootReadyPath, TimeSpan.FromSeconds(10));

        (int c1ExitCode, _) = RunClientWithRetries("c1-query", directory);
        Assert(c1ExitCode == C012ClientCli.ExitAccepted, "c1-query must be accepted");

        Guid sessionId = C012SessionPaths.Inspect(directory).SessionId!.Value;
        using (C012SessionSecret secret = C012SessionSecret.Load(C012SessionPaths.SessionSecretPath(directory)))
        {
            string pipeName = C012SessionPaths.DerivePipeName(sessionId);

            // A real, correctly-signed C2 request against the real, fully-functioning host --
            // severed immediately after the request is sent. The server keeps running the
            // real submitter-launch/wait/teardown sequence regardless of whether the client
            // is still there to see the outcome.
            SendRawSignedRequest(pipeName, secret, sessionId, 3, C012Control.C2, C012RequestType.SubmitConfig, readResponse: false);
        }

        (int submitterPid, _) = WaitForReadyRecord(submitterReadyPath, TimeSpan.FromSeconds(10));

        int hostExitCode = hostTask.GetAwaiter().GetResult();
        Assert(
            hostExitCode == C012HostCli.ExitTerminated,
            "the real server must still reach Terminated even though the client never read the C2 response");

        Assert(WaitUntilProcessIdIsGone((uint)submitterPid, TimeSpan.FromSeconds(10)), "the submitter must have run and be gone");
        Assert(WaitUntilProcessIdIsGone((uint)rootPid, TimeSpan.FromSeconds(10)), "the root must be gone after real teardown");

        AssertSessionFullyTornDown(directory, expectSecretDeleted: true);
    }
    finally
    {
        Directory.Delete(directory, recursive: true);
    }
}

static Process StartRealHostProcessForCrashTest(string sessionDir, string rootReadyPath, int idleTimeoutSeconds)
{
    string executable = RequireCurrentExecutable();
    var startInfo = new ProcessStartInfo
    {
        FileName = executable,
        UseShellExecute = false,
        CreateNoWindow = true,
    };

    if (Path.GetFileNameWithoutExtension(executable).Equals("dotnet", StringComparison.OrdinalIgnoreCase))
    {
        startInfo.ArgumentList.Add(Assembly.GetExecutingAssembly().Location);
    }

    startInfo.ArgumentList.Add("--run-real-host-for-testing");
    startInfo.ArgumentList.Add("--session-dir");
    startInfo.ArgumentList.Add(sessionDir);
    startInfo.ArgumentList.Add("--executable");
    startInfo.ArgumentList.Add(executable);
    startInfo.ArgumentList.Add("--expected-sha256");
    startInfo.ArgumentList.Add(ComputeSha256(executable));
    startInfo.ArgumentList.Add("--root-ready-path");
    startInfo.ArgumentList.Add(rootReadyPath);
    startInfo.ArgumentList.Add("--idle-timeout-seconds");
    startInfo.ArgumentList.Add(idleTimeoutSeconds.ToString(System.Globalization.CultureInfo.InvariantCulture));

    return Process.Start(startInfo) ?? throw new InvalidOperationException("Unable to start the real host process for testing.");
}

static string RequireNamedArg(string[] args, string flag)
{
    for (int index = 0; index < args.Length - 1; index++)
    {
        if (args[index] == flag)
        {
            return args[index + 1];
        }
    }

    throw new InvalidOperationException($"Missing required argument {flag} for --run-real-host-for-testing.");
}

static void WriteReadyRecord(string path)
{
    using Process current = Process.GetCurrentProcess();
    File.WriteAllText(
        path, $"{Environment.ProcessId}|{current.StartTime.ToUniversalTime().Ticks}", System.Text.Encoding.UTF8);
}

static (int Pid, long StartTimeTicks) WaitForReadyRecord(string path, TimeSpan timeout)
{
    Stopwatch timer = Stopwatch.StartNew();
    while (timer.Elapsed < timeout)
    {
        if (File.Exists(path))
        {
            string raw = File.ReadAllText(path, System.Text.Encoding.UTF8).Trim();
            string[] parts = raw.Split('|', 2, StringSplitOptions.RemoveEmptyEntries);
            if (parts.Length == 2 && int.TryParse(parts[0], out int pid) && long.TryParse(parts[1], out long startTicks))
            {
                return (pid, startTicks);
            }
        }

        System.Threading.Thread.Sleep(50);
    }

    throw new InvalidOperationException($"Ready record at '{path}' did not appear within {timeout}.");
}

static string WaitForPipeName(string directory, TimeSpan timeout)
{
    Stopwatch timer = Stopwatch.StartNew();
    while (timer.Elapsed < timeout)
    {
        if (C012SessionPaths.Inspect(directory).SessionId is { } sessionId)
        {
            return C012SessionPaths.DerivePipeName(sessionId);
        }

        System.Threading.Thread.Sleep(50);
    }

    throw new InvalidOperationException($"session.id never appeared under '{directory}' within {timeout}.");
}

// Bypasses C012ServerChannel/C012FrameCodec's own well-formedness by writing a completely
// unrelated (but still correctly length-prefixed) payload -- so the server can fully read the
// frame per its length header but fails to parse it as a C012WireRequest, which is the
// well-formed-frame-but-invalid-content path already proven at the unit level in B3
// (server_channel_rejects_malformed_frame_without_mutating_sequencer). An actually truncated
// frame is deliberately avoided here: the server's exact behavior for a header cut off
// mid-read is not something this test needs to depend on.
static void SendRawMalformedFrameAndDisconnect(string pipeName)
{
    using var pipe = new NamedPipeClientStream(".", pipeName, PipeDirection.InOut, PipeOptions.Asynchronous);
    using var connectTimeout = new CancellationTokenSource(TimeSpan.FromSeconds(10));
    pipe.ConnectAsync(connectTimeout.Token).GetAwaiter().GetResult();
    byte[] garbagePayload = System.Text.Encoding.UTF8.GetBytes("not-a-valid-wire-request");
    C012FrameCodec.WriteFrameAsync(pipe, garbagePayload, CancellationToken.None).GetAwaiter().GetResult();
}

// Hand-signs and hand-frames a request exactly like C012ClientChannel does internally, but
// against a real NamedPipeClientStream instead of an abstract Stream, and with full control
// over whether the response is ever read -- readResponse:false disposes the pipe immediately
// after the request is sent, simulating a client that dies or loses its connection right
// after a fully-formed, correctly-signed request left its hands but before any response
// arrives. C012ServerChannel.ProcessNextRequestAsync applies the request (mutating the real
// server's FSM) before it ever attempts to write the response, so this always exercises a
// real mutation on the real server, never a no-op.
static C012TransitionResult? SendRawSignedRequest(
    string pipeName,
    C012SessionSecret secret,
    Guid sessionId,
    long sequenceNumber,
    C012Control control,
    C012RequestType requestType,
    bool readResponse)
{
    string hmac = C012MessageAuthenticator.SignRequest(secret.Value, C012WireSchema.Version, sessionId, sequenceNumber, control, requestType);
    var request = new C012WireRequest(C012WireSchema.Version, sessionId, sequenceNumber, control, requestType, hmac);
    byte[] requestBytes = JsonSerializer.SerializeToUtf8Bytes(request, C012WireJsonOptions.Instance);

    using var pipe = new NamedPipeClientStream(".", pipeName, PipeDirection.InOut, PipeOptions.Asynchronous);
    using var connectTimeout = new CancellationTokenSource(TimeSpan.FromSeconds(10));
    pipe.ConnectAsync(connectTimeout.Token).GetAwaiter().GetResult();
    C012FrameCodec.WriteFrameAsync(pipe, requestBytes, CancellationToken.None).GetAwaiter().GetResult();

    if (!readResponse)
    {
        return null;
    }

    byte[]? responseFrame = C012FrameCodec.ReadFrameAsync(pipe, CancellationToken.None).GetAwaiter().GetResult();
    Assert(responseFrame is not null, "the real server must respond to a well-formed, correctly-sequenced raw request");
    C012WireResponse? response = JsonSerializer.Deserialize<C012WireResponse>(responseFrame!, C012WireJsonOptions.Instance);
    Assert(response is not null, "the raw response must parse as a well-formed C012WireResponse");
    Assert(C012MessageAuthenticator.VerifyResponse(secret.Value, response!), "the raw response must be authentically signed");
    return new C012TransitionResult(response!.Accepted, response!.ResultingState, response!.Reason);
}

// Shared final-verification step for every B4.4 scenario: session.id is a permanent record
// and must never disappear; session.sequence must always remain present and parseable, never
// corrupted, regardless of how the scenario ended; session.secret is deleted only on a
// graceful exit (the host's own finally block does that) -- a hard Kill() gives that finally
// block no chance to run, so expectSecretDeleted must be false for the one crash scenario.
// Root/submitter liveness is scenario-specific (different tests know different PIDs) and is
// asserted separately by each test, not here.
static void AssertSessionFullyTornDown(string directory, bool expectSecretDeleted)
{
    Assert(File.Exists(C012SessionPaths.SessionIdPath(directory)), "session.id must remain present as a permanent record");
    if (expectSecretDeleted)
    {
        Assert(!File.Exists(C012SessionPaths.SessionSecretPath(directory)), "session.secret must be deleted once the host loop exits gracefully");
    }

    C012SessionSequenceState? sequenceState = C012SessionSequenceCursor.TryReadSnapshot(directory);
    Assert(sequenceState is not null, "session.sequence must still be present and parseable, never corrupted");
}

static IReadOnlyList<string> SelfInvocationArguments(string executable, params string[] trailingArguments)
{
    var arguments = new List<string>();
    if (Path.GetFileNameWithoutExtension(executable).Equals("dotnet", StringComparison.OrdinalIgnoreCase))
    {
        arguments.Add(Assembly.GetExecutingAssembly().Location);
    }

    arguments.AddRange(trailingArguments);
    return arguments;
}

static string RequireCurrentExecutable() =>
    Environment.ProcessPath ?? throw new InvalidOperationException("Current process path is unavailable.");

static string ComputeSha256(string path)
{
    using FileStream stream = File.OpenRead(path);
    return Convert.ToHexString(SHA256.HashData(stream)).ToLowerInvariant();
}

static bool IsProcessRunning(uint processId)
{
    try
    {
        using Process process = Process.GetProcessById((int)processId);
        return !process.HasExited;
    }
    catch (ArgumentException)
    {
        return false;
    }
}

static bool WaitUntilProcessIdIsGone(uint processId, TimeSpan timeout)
{
    Stopwatch timer = Stopwatch.StartNew();
    while (timer.Elapsed < timeout)
    {
        if (!IsProcessRunning(processId))
        {
            return true;
        }

        System.Threading.Thread.Sleep(50);
    }

    return false;
}

static (int ExitCode, string Report) RunClientWithRetries(string verb, string directory)
{
    Exception? lastFailure = null;
    for (int attempt = 0; attempt < 50; attempt++)
    {
        using var stdout = new StringWriter();
        using var stderr = new StringWriter();
        try
        {
            int exitCode = C012ClientCli.Run(verb, ["--session-dir", directory], stdout, stderr);
            if (exitCode != C012ClientCli.ExitTransportFailure)
            {
                return (exitCode, stdout.ToString());
            }

            lastFailure = new InvalidOperationException(stderr.ToString());
        }
        catch (Exception exception)
        {
            lastFailure = exception;
        }

        System.Threading.Thread.Sleep(100);
    }

    throw new InvalidOperationException("The host never became ready to accept a connection.", lastFailure);
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

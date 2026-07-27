using System.Text.Json;
using TradeJournal.Lab.Mt5WizardAutomation;

var tests = new (string Name, Func<Task> Body)[]
{
    ("happy_path_selects_one_exact_server_and_returns_censused_servers", HappyPath),
    ("invalid_request_fails_without_touching_driver", InvalidRequestDoesNotTouchDriver),
    ("empty_broker_results_fail_closed_and_abort", MissingBrokerFailsClosed),
    ("missing_exact_server_fails_closed_and_aborts", MissingServerFailsClosed),
    ("multiple_brokers_select_unique_exact_server", MultipleBrokersSelectUniquePair),
    ("duplicate_exact_servers_across_brokers_fail_closed", DuplicateBrokerServerPairFailsClosed),
    ("duplicate_exact_servers_with_same_label_fail_closed", DuplicateBrokerFailsClosed),
    ("suggested_broker_label_is_not_authoritative", SuggestedBrokerLabelIsNotAuthoritative),
    ("invalid_candidate_fails_closed", InvalidCandidateFailsClosed),
    ("empty_censused_servers_fail_closed", EmptyServersFailClosed),
    ("invalid_censused_server_name_fails_closed", InvalidServerNameFailsClosed),
    ("post_wizard_server_must_match_requested_server", PostWizardServerMustMatch),
    ("driver_exception_is_sanitized_and_fails_closed", DriverExceptionIsSanitized),
    ("step_timeout_fails_closed", StepTimeoutFailsClosed),
    ("caller_cancellation_fails_closed", CallerCancellationFailsClosed),
    ("orchestrator_instance_cannot_be_reused", OrchestratorCannotBeReused),
    ("valid_ui_profile_is_accepted", ValidUiProfileIsAccepted),
    ("ui_profile_rejects_single_selector_without_identity", UiProfileRejectsMissingIdentity),
    ("ui_profile_rejects_control_characters", UiProfileRejectsControlCharacters),
    ("flaui_attach_refuses_before_inspecting_pid", FlaUiAttachIsHardDisabled),
    ("actual_ui_automation_gate_is_hard_disabled", ActualAutomationIsHardDisabled),
};

int failures = 0;
foreach ((string name, Func<Task> body) in tests)
{
    try
    {
        await body().ConfigureAwait(false);
        Console.WriteLine($"PASS {name}");
    }
    catch (Exception exception)
    {
        failures++;
        Console.Error.WriteLine(
            $"FAIL {name}: {exception.GetType().Name}: {exception.Message}");
        Console.Error.WriteLine(exception.StackTrace);
    }
}

return failures == 0 ? 0 : 1;

static async Task HappyPath()
{
    var driver = FakeDriver.Success();
    var orchestrator = new BrokerWizardOrchestrator(driver);

    BrokerWizardOutcome outcome = await orchestrator.RunAsync(Request()).ConfigureAwait(false);

    Assert(outcome.Succeeded, "outcome should succeed");
    Assert(outcome.FinalState == BrokerWizardState.Completed, "final state");
    Assert(outcome.FailureReason == BrokerWizardFailureReason.None, "failure reason");
    Assert(outcome.SelectedBrokerLabel == "GoatFundedTrader", "selected broker");
    Assert(
        outcome.CensusedServerNames.SequenceEqual(
            ["GoatFundedTrader-Demo", "GoatFundedTrader-Live"]),
        "normalized server names");
    Assert(
        driver.Calls.SequenceEqual(
        [
            "wait",
            "open",
            "search:GoatFundedTrader",
            "read_candidates",
            "select:broker-1",
            "confirm",
            "read_servers",
        ]),
        "driver call order");
    Assert(
        outcome.Transitions.Select(transition => transition.To).SequenceEqual(
        [
            BrokerWizardState.WaitingForTerminal,
            BrokerWizardState.OpeningBrokerWizard,
            BrokerWizardState.SearchingBroker,
            BrokerWizardState.SelectingBroker,
            BrokerWizardState.ConfirmingSelection,
            BrokerWizardState.ReadingCensusedServers,
            BrokerWizardState.Completed,
        ]),
        "transition order");
}

static async Task InvalidRequestDoesNotTouchDriver()
{
    var driver = FakeDriver.Success();
    var orchestrator = new BrokerWizardOrchestrator(driver);

    BrokerWizardOutcome outcome = await orchestrator.RunAsync(
        new BrokerWizardRequest(
            "Goat\nFunded",
            "GoatFundedTrader",
            "GoatFundedTrader-Live",
            TimeSpan.FromSeconds(1)))
        .ConfigureAwait(false);

    Assert(!outcome.Succeeded, "outcome should fail");
    Assert(outcome.FailureReason == BrokerWizardFailureReason.InvalidRequest, "invalid request");
    Assert(driver.Calls.Count == 0, "driver must not be touched");
}

static async Task MissingBrokerFailsClosed()
{
    var driver = FakeDriver.Success();
    driver.Candidates = [];

    BrokerWizardOutcome outcome =
        await new BrokerWizardOrchestrator(driver).RunAsync(Request()).ConfigureAwait(false);

    AssertFailure(outcome, BrokerWizardFailureReason.BrokerNotFound);
    Assert(driver.AbortCount == 1, "abort once");
    Assert(driver.Calls[^1] == "abort", "abort last");
}

static async Task MissingServerFailsClosed()
{
    var driver = FakeDriver.Success();
    driver.Candidates =
    [
        new BrokerWizardCandidate(
            "broker-1",
            "GoatFundedTrader",
            ["GoatFundedTrader-Demo"]),
    ];

    BrokerWizardOutcome outcome =
        await new BrokerWizardOrchestrator(driver).RunAsync(Request()).ConfigureAwait(false);

    AssertFailure(outcome, BrokerWizardFailureReason.ServerNotFound);
    Assert(driver.AbortCount == 1, "abort once");
}

static async Task MultipleBrokersSelectUniquePair()
{
    var driver = FakeDriver.Success();
    driver.Candidates =
    [
        new BrokerWizardCandidate(
            "broker-demo",
            "GoatFundedTrader",
            ["GoatFundedTrader-Demo"]),
        new BrokerWizardCandidate(
            "broker-live",
            "GoatFundedTrader",
            ["GoatFundedTrader-Live"]),
        new BrokerWizardCandidate(
            "other",
            "Other Broker",
            ["OtherBroker-Live"]),
    ];

    BrokerWizardOutcome outcome =
        await new BrokerWizardOrchestrator(driver).RunAsync(Request()).ConfigureAwait(false);

    Assert(outcome.Succeeded, "unique pair should succeed");
    Assert(driver.Calls.Contains("select:broker-live"), "correct candidate selected");
}

static async Task SuggestedBrokerLabelIsNotAuthoritative()
{
    var driver = FakeDriver.Success();
    driver.Candidates =
    [
        new BrokerWizardCandidate(
            "broker-1",
            "Goat Funded Trader",
            ["GoatFundedTrader-Live"]),
    ];
    var request = new BrokerWizardRequest(
        "Goat Funded",
        "Goat Funded LTD",
        "GoatFundedTrader-Live",
        TimeSpan.FromSeconds(1));

    BrokerWizardOutcome outcome =
        await new BrokerWizardOrchestrator(driver).RunAsync(request).ConfigureAwait(false);

    Assert(outcome.Succeeded, "the exact server, not the AI label, is authoritative");
    Assert(outcome.SelectedBrokerLabel == "Goat Funded Trader", "MT5 label is published");
}

static async Task DuplicateBrokerServerPairFailsClosed()
{
    var driver = FakeDriver.Success();
    driver.Candidates =
    [
        new BrokerWizardCandidate(
            "broker-1",
            "GoatFundedTrader",
            ["GoatFundedTrader-Live"]),
        new BrokerWizardCandidate(
            "broker-2",
            "goatfundedtrader",
            ["goatfundedtrader-live"]),
    ];

    BrokerWizardOutcome outcome =
        await new BrokerWizardOrchestrator(driver).RunAsync(Request()).ConfigureAwait(false);

    AssertFailure(outcome, BrokerWizardFailureReason.AmbiguousBroker);
}

static async Task DuplicateBrokerFailsClosed()
{
    var driver = FakeDriver.Success();
    driver.Candidates =
    [
        new BrokerWizardCandidate(
            "broker-1",
            "GoatFundedTrader",
            ["GoatFundedTrader-Live"]),
        new BrokerWizardCandidate(
            "broker-2",
            "goatfundedtrader",
            ["GoatFundedTrader-Live"]),
    ];

    BrokerWizardOutcome outcome =
        await new BrokerWizardOrchestrator(driver).RunAsync(Request()).ConfigureAwait(false);

    AssertFailure(outcome, BrokerWizardFailureReason.AmbiguousBroker);
    Assert(driver.AbortCount == 1, "abort once");
}

static async Task InvalidCandidateFailsClosed()
{
    var driver = FakeDriver.Success();
    driver.Candidates =
    [
        new BrokerWizardCandidate(
            "broker-1",
            "GoatFundedTrader",
            ["GoatFundedTrader-Live"]),
        new BrokerWizardCandidate(
            "",
            "Untrusted",
            ["GoatFundedTrader-Live"]),
    ];

    BrokerWizardOutcome outcome =
        await new BrokerWizardOrchestrator(driver).RunAsync(Request()).ConfigureAwait(false);

    AssertFailure(outcome, BrokerWizardFailureReason.AmbiguousBroker);
}

static async Task PostWizardServerMustMatch()
{
    var driver = FakeDriver.Success();
    driver.ServerNames = ["GoatFundedTrader-Demo"];

    BrokerWizardOutcome outcome =
        await new BrokerWizardOrchestrator(driver).RunAsync(Request()).ConfigureAwait(false);

    AssertFailure(outcome, BrokerWizardFailureReason.VerificationFailure);
}

static async Task EmptyServersFailClosed()
{
    var driver = FakeDriver.Success();
    driver.ServerNames = [];

    BrokerWizardOutcome outcome =
        await new BrokerWizardOrchestrator(driver).RunAsync(Request()).ConfigureAwait(false);

    AssertFailure(outcome, BrokerWizardFailureReason.NoCensusedServers);
    Assert(driver.AbortCount == 1, "abort once");
}

static async Task InvalidServerNameFailsClosed()
{
    var driver = FakeDriver.Success();
    driver.ServerNames = ["GoatFundedTrader-Live", "bad\u0000server"];

    BrokerWizardOutcome outcome =
        await new BrokerWizardOrchestrator(driver).RunAsync(Request()).ConfigureAwait(false);

    AssertFailure(outcome, BrokerWizardFailureReason.VerificationFailure);
}

static async Task DriverExceptionIsSanitized()
{
    const string sensitiveMarker = "password=must-not-leak";
    var driver = FakeDriver.Success();
    driver.ThrowAt = "confirm";
    driver.ExceptionMessage = sensitiveMarker;

    BrokerWizardOutcome outcome =
        await new BrokerWizardOrchestrator(driver).RunAsync(Request()).ConfigureAwait(false);
    string serialized = JsonSerializer.Serialize(outcome);

    AssertFailure(outcome, BrokerWizardFailureReason.DriverFailure);
    Assert(!serialized.Contains(sensitiveMarker, StringComparison.Ordinal), "exception must be sanitized");
    Assert(!serialized.Contains("password", StringComparison.OrdinalIgnoreCase), "no password field");
}

static async Task StepTimeoutFailsClosed()
{
    var driver = FakeDriver.Success();
    driver.BlockAt = "wait";
    var request = new BrokerWizardRequest(
        "GoatFundedTrader",
        "GoatFundedTrader",
        "GoatFundedTrader-Live",
        TimeSpan.FromMilliseconds(25));

    BrokerWizardOutcome outcome =
        await new BrokerWizardOrchestrator(driver).RunAsync(request).ConfigureAwait(false);

    AssertFailure(outcome, BrokerWizardFailureReason.Timeout);
    Assert(driver.AbortCount == 1, "abort once");
}

static async Task CallerCancellationFailsClosed()
{
    var driver = FakeDriver.Success();
    driver.BlockAt = "wait";
    using var cancellation = new CancellationTokenSource();
    cancellation.Cancel();

    BrokerWizardOutcome outcome = await new BrokerWizardOrchestrator(driver)
        .RunAsync(Request(), cancellation.Token)
        .ConfigureAwait(false);

    AssertFailure(outcome, BrokerWizardFailureReason.Cancelled);
}

static async Task OrchestratorCannotBeReused()
{
    var orchestrator = new BrokerWizardOrchestrator(FakeDriver.Success());
    BrokerWizardOutcome first = await orchestrator.RunAsync(Request()).ConfigureAwait(false);
    Assert(first.Succeeded, "first run");

    await AssertThrowsAsync<InvalidOperationException>(
        () => orchestrator.RunAsync(Request()),
        "second run must throw").ConfigureAwait(false);
}

static Task ValidUiProfileIsAccepted()
{
    Profile().Validate();
    return Task.CompletedTask;
}

static Task UiProfileRejectsMissingIdentity()
{
    Mt5WizardUiProfile profile = Profile() with
    {
        MainWindow = new Mt5UiSelector("Window"),
    };
    AssertThrows<InvalidOperationException>(
        profile.Validate,
        "single selector without identity");
    return Task.CompletedTask;
}

static Task UiProfileRejectsControlCharacters()
{
    Mt5WizardUiProfile profile = Profile() with
    {
        SearchCommand = new Mt5UiSelector("Button", Name: "Find\nBroker"),
    };
    AssertThrows<InvalidOperationException>(
        profile.Validate,
        "control character");
    return Task.CompletedTask;
}

static Task FlaUiAttachIsHardDisabled()
{
    InvalidOperationException exception = CaptureThrows<InvalidOperationException>(
        () => FlaUiBrokerWizardDriver.Attach(int.MaxValue, Profile()),
        "FlaUI attach must be hard-disabled");
    Assert(
        exception.Message.Contains("HARD_DISABLED", StringComparison.Ordinal),
        "gate must run before PID inspection");
    return Task.CompletedTask;
}

static Task ActualAutomationIsHardDisabled()
{
    Assert(!Mt5WizardRuntimeGate.ActualUiAutomationEnabled, "actual UI gate");
    AssertThrows<InvalidOperationException>(
        Mt5WizardRuntimeGate.DemandActualUiAutomation,
        "actual UI demand");
    return Task.CompletedTask;
}

static BrokerWizardRequest Request() =>
    new(
        "GoatFundedTrader",
        "GoatFundedTrader",
        "GoatFundedTrader-Live",
        TimeSpan.FromSeconds(1));

static Mt5WizardUiProfile Profile() =>
    new(
        "mt5-ui-profile-v1",
        new Mt5UiSelector("Window", AutomationId: "terminal-main"),
        new Mt5UiSelector("MenuItem", AutomationId: "find-broker"),
        new Mt5UiSelector("Window", AutomationId: "broker-wizard"),
        new Mt5UiSelector("Edit", AutomationId: "broker-search"),
        new Mt5UiSelector("Button", AutomationId: "search"),
        new Mt5UiSelector("List", AutomationId: "broker-results"),
        new Mt5UiSelector("ListItem"),
        new Mt5UiSelector("Text", AutomationId: "broker-label"),
        new Mt5UiSelector("ListItem"),
        new Mt5UiSelector("Text", AutomationId: "broker-server-label"),
        new Mt5UiSelector("Button", AutomationId: "confirm"),
        new Mt5UiSelector("List", AutomationId: "servers"),
        new Mt5UiSelector("ListItem"),
        new Mt5UiSelector("Text", AutomationId: "server-label"),
        new Mt5UiSelector("Button", AutomationId: "cancel"));

static void AssertFailure(
    BrokerWizardOutcome outcome,
    BrokerWizardFailureReason expectedReason)
{
    Assert(!outcome.Succeeded, "outcome should fail");
    Assert(outcome.FinalState == BrokerWizardState.FailedClosed, "failed-closed state");
    Assert(outcome.FailureReason == expectedReason, "failure reason");
    Assert(outcome.SelectedBrokerLabel is null, "no selected broker publication");
    Assert(outcome.CensusedServerNames.Count == 0, "no server publication");
}

static void Assert(bool condition, string message)
{
    if (!condition)
    {
        throw new InvalidOperationException(message);
    }
}

static void AssertThrows<TException>(Action action, string message)
    where TException : Exception
{
    try
    {
        action();
    }
    catch (TException)
    {
        return;
    }

    throw new InvalidOperationException(message);
}

static TException CaptureThrows<TException>(Action action, string message)
    where TException : Exception
{
    try
    {
        action();
    }
    catch (TException exception)
    {
        return exception;
    }

    throw new InvalidOperationException(message);
}

static async Task AssertThrowsAsync<TException>(
    Func<Task> action,
    string message)
    where TException : Exception
{
    try
    {
        await action().ConfigureAwait(false);
    }
    catch (TException)
    {
        return;
    }

    throw new InvalidOperationException(message);
}

internal sealed class FakeDriver : IMt5BrokerWizardDriver
{
    public List<string> Calls { get; } = [];

    public IReadOnlyList<BrokerWizardCandidate> Candidates { get; set; } = [];

    public IReadOnlyList<string> ServerNames { get; set; } = [];

    public string? ThrowAt { get; set; }

    public string ExceptionMessage { get; set; } = "driver failed";

    public string? BlockAt { get; set; }

    public int AbortCount { get; private set; }

    public static FakeDriver Success() =>
        new()
        {
            Candidates =
            [
                new BrokerWizardCandidate(
                    "broker-1",
                    "GoatFundedTrader",
                    ["GoatFundedTrader-Live"]),
            ],
            ServerNames =
            [
                "GoatFundedTrader-Live",
                "GoatFundedTrader-Demo",
                "goatfundedtrader-live",
            ],
        };

    public Task WaitForTerminalAsync(CancellationToken cancellationToken) =>
        StepAsync("wait", cancellationToken);

    public Task OpenFindBrokerAsync(CancellationToken cancellationToken) =>
        StepAsync("open", cancellationToken);

    public Task SearchBrokerAsync(string searchText, CancellationToken cancellationToken) =>
        StepAsync($"search:{searchText}", cancellationToken, "search");

    public async Task<IReadOnlyList<BrokerWizardCandidate>> ReadBrokerCandidatesAsync(
        CancellationToken cancellationToken)
    {
        await StepAsync("read_candidates", cancellationToken).ConfigureAwait(false);
        return Candidates;
    }

    public Task SelectBrokerAsync(
        BrokerWizardCandidate candidate,
        CancellationToken cancellationToken) =>
        StepAsync($"select:{candidate.StableId}", cancellationToken, "select");

    public Task ConfirmBrokerSelectionAsync(CancellationToken cancellationToken) =>
        StepAsync("confirm", cancellationToken);

    public async Task<IReadOnlyList<string>> ReadCensusedServerNamesAsync(
        CancellationToken cancellationToken)
    {
        await StepAsync("read_servers", cancellationToken).ConfigureAwait(false);
        return ServerNames;
    }

    public Task AbortAsync(CancellationToken cancellationToken)
    {
        AbortCount++;
        Calls.Add("abort");
        return Task.CompletedTask;
    }

    private async Task StepAsync(
        string call,
        CancellationToken cancellationToken,
        string? stepName = null)
    {
        Calls.Add(call);
        string name = stepName ?? call;
        if (ThrowAt == name)
        {
            throw new InvalidOperationException(ExceptionMessage);
        }

        if (BlockAt == name)
        {
            await Task.Delay(Timeout.InfiniteTimeSpan, cancellationToken).ConfigureAwait(false);
        }
    }
}

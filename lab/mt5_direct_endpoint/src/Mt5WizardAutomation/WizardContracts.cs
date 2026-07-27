namespace TradeJournal.Lab.Mt5WizardAutomation;

public enum BrokerWizardState
{
    NotStarted,
    WaitingForTerminal,
    OpeningBrokerWizard,
    SearchingBroker,
    SelectingBroker,
    ConfirmingSelection,
    ReadingCensusedServers,
    Completed,
    FailedClosed,
}

public enum BrokerWizardFailureReason
{
    None,
    InvalidRequest,
    BrokerNotFound,
    ServerNotFound,
    AmbiguousBroker,
    NoCensusedServers,
    Timeout,
    Cancelled,
    DriverFailure,
    VerificationFailure,
}

public sealed record BrokerWizardRequest(
    string SearchText,
    string? SuggestedBrokerLabel,
    string ExpectedServerName,
    TimeSpan StepTimeout);

public sealed record BrokerWizardCandidate(
    string StableId,
    string BrokerLabel,
    IReadOnlyList<string> ServerNames);

public sealed record BrokerWizardTransition(
    BrokerWizardState From,
    BrokerWizardState To);

public sealed record BrokerWizardOutcome(
    bool Succeeded,
    BrokerWizardState FinalState,
    BrokerWizardFailureReason FailureReason,
    string? SelectedBrokerLabel,
    IReadOnlyList<string> CensusedServerNames,
    IReadOnlyList<BrokerWizardTransition> Transitions)
{
    internal static BrokerWizardOutcome Success(
        string selectedBrokerLabel,
        IReadOnlyList<string> censusedServerNames,
        IReadOnlyList<BrokerWizardTransition> transitions) =>
        new(
            true,
            BrokerWizardState.Completed,
            BrokerWizardFailureReason.None,
            selectedBrokerLabel,
            censusedServerNames,
            transitions);

    internal static BrokerWizardOutcome Failure(
        BrokerWizardFailureReason reason,
        IReadOnlyList<BrokerWizardTransition> transitions) =>
        new(
            false,
            BrokerWizardState.FailedClosed,
            reason,
            null,
            Array.Empty<string>(),
            transitions);
}

public interface IMt5BrokerWizardDriver
{
    Task WaitForTerminalAsync(CancellationToken cancellationToken);

    Task OpenFindBrokerAsync(CancellationToken cancellationToken);

    Task SearchBrokerAsync(string searchText, CancellationToken cancellationToken);

    Task<IReadOnlyList<BrokerWizardCandidate>> ReadBrokerCandidatesAsync(
        CancellationToken cancellationToken);

    Task SelectBrokerAsync(
        BrokerWizardCandidate candidate,
        CancellationToken cancellationToken);

    Task ConfirmBrokerSelectionAsync(CancellationToken cancellationToken);

    Task<IReadOnlyList<string>> ReadCensusedServerNamesAsync(
        CancellationToken cancellationToken);

    Task AbortAsync(CancellationToken cancellationToken);
}

public static class Mt5WizardRuntimeGate
{
    public const bool ActualUiAutomationEnabled = false;

    public static void DemandActualUiAutomation()
    {
        if (!ActualUiAutomationEnabled)
        {
            throw new InvalidOperationException(
                "Actual MT5 UI automation is HARD_DISABLED in this build.");
        }
    }
}

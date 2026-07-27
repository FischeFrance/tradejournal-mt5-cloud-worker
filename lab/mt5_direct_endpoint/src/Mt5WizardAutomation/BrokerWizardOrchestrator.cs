namespace TradeJournal.Lab.Mt5WizardAutomation;

public sealed class BrokerWizardOrchestrator
{
    private static readonly TimeSpan MinimumStepTimeout = TimeSpan.FromMilliseconds(10);
    private static readonly TimeSpan MaximumStepTimeout = TimeSpan.FromMinutes(5);
    private static readonly TimeSpan AbortTimeout = TimeSpan.FromSeconds(2);
    private const int MaximumLabelLength = 256;

    private readonly IMt5BrokerWizardDriver _driver;
    private readonly List<BrokerWizardTransition> _transitions = [];
    private BrokerWizardState _state = BrokerWizardState.NotStarted;

    public BrokerWizardOrchestrator(IMt5BrokerWizardDriver driver)
    {
        ArgumentNullException.ThrowIfNull(driver);
        _driver = driver;
    }

    public async Task<BrokerWizardOutcome> RunAsync(
        BrokerWizardRequest request,
        CancellationToken cancellationToken = default)
    {
        ArgumentNullException.ThrowIfNull(request);
        if (_state != BrokerWizardState.NotStarted)
        {
            throw new InvalidOperationException(
                "A BrokerWizardOrchestrator instance can execute only one run.");
        }

        if (!IsValidLabel(request.SearchText)
            || (request.SuggestedBrokerLabel is not null
                && !IsValidLabel(request.SuggestedBrokerLabel))
            || !IsValidLabel(request.ExpectedServerName)
            || request.StepTimeout < MinimumStepTimeout
            || request.StepTimeout > MaximumStepTimeout)
        {
            return FailWithoutDriver(BrokerWizardFailureReason.InvalidRequest);
        }

        try
        {
            MoveTo(BrokerWizardState.WaitingForTerminal);
            await ExecuteStepAsync(
                token => _driver.WaitForTerminalAsync(token),
                request.StepTimeout,
                cancellationToken).ConfigureAwait(false);

            MoveTo(BrokerWizardState.OpeningBrokerWizard);
            await ExecuteStepAsync(
                token => _driver.OpenFindBrokerAsync(token),
                request.StepTimeout,
                cancellationToken).ConfigureAwait(false);

            MoveTo(BrokerWizardState.SearchingBroker);
            await ExecuteStepAsync(
                token => _driver.SearchBrokerAsync(request.SearchText.Trim(), token),
                request.StepTimeout,
                cancellationToken).ConfigureAwait(false);

            IReadOnlyList<BrokerWizardCandidate> candidates = await ExecuteStepAsync(
                token => _driver.ReadBrokerCandidatesAsync(token),
                request.StepTimeout,
                cancellationToken).ConfigureAwait(false);

            BrokerWizardCandidate? selected = SelectUniqueServerCandidate(
                candidates,
                request.ExpectedServerName,
                out BrokerWizardFailureReason selectionFailure);
            if (selected is null)
            {
                return await FailAndAbortAsync(selectionFailure).ConfigureAwait(false);
            }

            MoveTo(BrokerWizardState.SelectingBroker);
            await ExecuteStepAsync(
                token => _driver.SelectBrokerAsync(selected, token),
                request.StepTimeout,
                cancellationToken).ConfigureAwait(false);

            MoveTo(BrokerWizardState.ConfirmingSelection);
            await ExecuteStepAsync(
                token => _driver.ConfirmBrokerSelectionAsync(token),
                request.StepTimeout,
                cancellationToken).ConfigureAwait(false);

            MoveTo(BrokerWizardState.ReadingCensusedServers);
            IReadOnlyList<string> reportedServerNames = await ExecuteStepAsync(
                token => _driver.ReadCensusedServerNamesAsync(token),
                request.StepTimeout,
                cancellationToken).ConfigureAwait(false);

            string[]? censusedServerNames = NormalizeServerNames(reportedServerNames);
            if (censusedServerNames is null || censusedServerNames.Length == 0)
            {
                BrokerWizardFailureReason reason = reportedServerNames.Count == 0
                    ? BrokerWizardFailureReason.NoCensusedServers
                    : BrokerWizardFailureReason.VerificationFailure;
                return await FailAndAbortAsync(reason).ConfigureAwait(false);
            }

            if (!censusedServerNames.Contains(
                request.ExpectedServerName.Trim(),
                StringComparer.OrdinalIgnoreCase))
            {
                return await FailAndAbortAsync(
                    BrokerWizardFailureReason.VerificationFailure).ConfigureAwait(false);
            }

            MoveTo(BrokerWizardState.Completed);
            return BrokerWizardOutcome.Success(
                selected.BrokerLabel.Trim(),
                censusedServerNames,
                _transitions.ToArray());
        }
        catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
        {
            return await FailAndAbortAsync(BrokerWizardFailureReason.Cancelled).ConfigureAwait(false);
        }
        catch (OperationCanceledException)
        {
            return await FailAndAbortAsync(BrokerWizardFailureReason.Timeout).ConfigureAwait(false);
        }
        catch (Exception)
        {
            return await FailAndAbortAsync(BrokerWizardFailureReason.DriverFailure).ConfigureAwait(false);
        }
    }

    private static bool IsValidLabel(string? value)
    {
        if (string.IsNullOrWhiteSpace(value) || value.Length > MaximumLabelLength)
        {
            return false;
        }

        return value.All(character => !char.IsControl(character));
    }

    private static BrokerWizardCandidate? SelectUniqueServerCandidate(
        IReadOnlyList<BrokerWizardCandidate>? candidates,
        string expectedServerName,
        out BrokerWizardFailureReason failureReason)
    {
        if (candidates is null)
        {
            failureReason = BrokerWizardFailureReason.VerificationFailure;
            return null;
        }

        string expectedServer = expectedServerName.Trim();
        bool invalidCandidate = candidates.Any(candidate =>
            candidate is null
            || !IsValidLabel(candidate.StableId)
            || !IsValidLabel(candidate.BrokerLabel)
            || candidate.ServerNames is null
            || candidate.ServerNames.Any(serverName => !IsValidLabel(serverName)));
        if (invalidCandidate)
        {
            failureReason = BrokerWizardFailureReason.AmbiguousBroker;
            return null;
        }

        if (candidates.Count == 0)
        {
            failureReason = BrokerWizardFailureReason.BrokerNotFound;
            return null;
        }

        BrokerWizardCandidate[] exact = candidates
            .Where(candidate =>
                candidate.ServerNames.Any(serverName =>
                    serverName.Trim().Equals(
                        expectedServer,
                        StringComparison.OrdinalIgnoreCase)))
            .ToArray();
        if (exact.Length == 0)
        {
            failureReason = BrokerWizardFailureReason.ServerNotFound;
            return null;
        }

        if (exact.Length != 1)
        {
            failureReason = BrokerWizardFailureReason.AmbiguousBroker;
            return null;
        }

        failureReason = BrokerWizardFailureReason.None;
        return exact[0];
    }

    private static string[]? NormalizeServerNames(
        IReadOnlyList<string>? reportedServerNames)
    {
        if (reportedServerNames is null
            || reportedServerNames.Any(serverName => !IsValidLabel(serverName)))
        {
            return null;
        }

        return reportedServerNames
            .Select(serverName => serverName.Trim())
            .Distinct(StringComparer.OrdinalIgnoreCase)
            .OrderBy(serverName => serverName, StringComparer.OrdinalIgnoreCase)
            .ToArray();
    }

    private async Task<BrokerWizardOutcome> FailAndAbortAsync(
        BrokerWizardFailureReason reason)
    {
        MoveTo(BrokerWizardState.FailedClosed);
        using var abortTimeout = new CancellationTokenSource(AbortTimeout);
        try
        {
            await _driver.AbortAsync(abortTimeout.Token).ConfigureAwait(false);
        }
        catch (Exception)
        {
            // Abort is best-effort after the public outcome has already failed closed.
            // A driver exception must never restore or upgrade the outcome.
        }

        return BrokerWizardOutcome.Failure(reason, _transitions.ToArray());
    }

    private BrokerWizardOutcome FailWithoutDriver(BrokerWizardFailureReason reason)
    {
        MoveTo(BrokerWizardState.FailedClosed);
        return BrokerWizardOutcome.Failure(reason, _transitions.ToArray());
    }

    private void MoveTo(BrokerWizardState next)
    {
        _transitions.Add(new BrokerWizardTransition(_state, next));
        _state = next;
    }

    private static async Task ExecuteStepAsync(
        Func<CancellationToken, Task> operation,
        TimeSpan stepTimeout,
        CancellationToken callerToken)
    {
        using var timeout = CancellationTokenSource.CreateLinkedTokenSource(callerToken);
        timeout.CancelAfter(stepTimeout);
        await operation(timeout.Token).ConfigureAwait(false);
    }

    private static async Task<T> ExecuteStepAsync<T>(
        Func<CancellationToken, Task<T>> operation,
        TimeSpan stepTimeout,
        CancellationToken callerToken)
    {
        using var timeout = CancellationTokenSource.CreateLinkedTokenSource(callerToken);
        timeout.CancelAfter(stepTimeout);
        return await operation(timeout.Token).ConfigureAwait(false);
    }
}

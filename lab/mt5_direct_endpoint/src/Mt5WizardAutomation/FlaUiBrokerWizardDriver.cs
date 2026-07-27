using FlaUI.Core;
using FlaUI.Core.AutomationElements;
using FlaUI.UIA3;

namespace TradeJournal.Lab.Mt5WizardAutomation;

public sealed class FlaUiBrokerWizardDriver : IMt5BrokerWizardDriver, IDisposable
{
    private static readonly TimeSpan PollInterval = TimeSpan.FromMilliseconds(100);

    private readonly Application _application;
    private readonly UIA3Automation _automation;
    private readonly Mt5WizardUiProfile _profile;
    private readonly Dictionary<string, AutomationElement> _candidateElements =
        new(StringComparer.Ordinal);

    private AutomationElement? _mainWindow;
    private AutomationElement? _wizardWindow;
    private bool _disposed;

    private FlaUiBrokerWizardDriver(
        Application application,
        UIA3Automation automation,
        Mt5WizardUiProfile profile)
    {
        _application = application;
        _automation = automation;
        _profile = profile;
    }

    public static FlaUiBrokerWizardDriver Attach(
        int processId,
        Mt5WizardUiProfile profile)
    {
        // This check must remain first: a disabled build cannot even inspect whether a
        // caller-supplied PID exists, much less attach UI Automation to it.
        Mt5WizardRuntimeGate.DemandActualUiAutomation();

        ArgumentOutOfRangeException.ThrowIfNegativeOrZero(processId);
        ArgumentNullException.ThrowIfNull(profile);
        profile.Validate();
        if (!OperatingSystem.IsWindows())
        {
            throw new PlatformNotSupportedException(
                "FlaUI MT5 automation requires Windows.");
        }

        Application application = Application.Attach(processId);
        try
        {
            var automation = new UIA3Automation();
            return new FlaUiBrokerWizardDriver(application, automation, profile);
        }
        catch
        {
            application.Dispose();
            throw;
        }
    }

    public async Task WaitForTerminalAsync(CancellationToken cancellationToken)
    {
        ThrowIfDisposed();
        _mainWindow = await WaitForSingleAsync(
            () =>
            {
                Window? window = _application.GetMainWindow(_automation);
                return window is null
                    ? Array.Empty<AutomationElement>()
                    : Match(window, _profile.MainWindow)
                        ? [window]
                        : Array.Empty<AutomationElement>();
            },
            "terminal main window",
            cancellationToken).ConfigureAwait(false);
    }

    public async Task OpenFindBrokerAsync(CancellationToken cancellationToken)
    {
        AutomationElement mainWindow = RequireMainWindow();
        AutomationElement command = await WaitForSingleAsync(
            () => FindMatches(mainWindow, _profile.FindBrokerCommand),
            "Find your broker command",
            cancellationToken).ConfigureAwait(false);
        Invoke(command, "Find your broker command");

        _wizardWindow = await WaitForSingleAsync(
            () => FindMatches(_automation.GetDesktop(), _profile.WizardWindow),
            "broker wizard window",
            cancellationToken).ConfigureAwait(false);
    }

    public async Task SearchBrokerAsync(
        string searchText,
        CancellationToken cancellationToken)
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(searchText);
        AutomationElement wizard = RequireWizardWindow();
        AutomationElement searchBox = await WaitForSingleAsync(
            () => FindMatches(wizard, _profile.BrokerSearchBox),
            "broker search box",
            cancellationToken).ConfigureAwait(false);
        SetValue(searchBox, searchText, "broker search box");

        AutomationElement searchCommand = await WaitForSingleAsync(
            () => FindMatches(wizard, _profile.SearchCommand),
            "broker search command",
            cancellationToken).ConfigureAwait(false);
        Invoke(searchCommand, "broker search command");
    }

    public async Task<IReadOnlyList<BrokerWizardCandidate>> ReadBrokerCandidatesAsync(
        CancellationToken cancellationToken)
    {
        AutomationElement wizard = RequireWizardWindow();
        AutomationElement container = await WaitForSingleAsync(
            () => FindMatches(wizard, _profile.ResultsContainer),
            "broker results container",
            cancellationToken).ConfigureAwait(false);
        AutomationElement[] items = FindMatches(container, _profile.BrokerResultItem);

        _candidateElements.Clear();
        var candidates = new List<BrokerWizardCandidate>();
        foreach (AutomationElement item in items)
        {
            AutomationElement[] labels = FindMatches(item, _profile.BrokerResultLabel);
            if (labels.Length != 1 || string.IsNullOrWhiteSpace(labels[0].Name))
            {
                throw new InvalidOperationException(
                    "A broker result did not expose exactly one safe label.");
            }

            string stableId = Guid.NewGuid().ToString("N");
            _candidateElements.Add(stableId, item);
            AutomationElement[] serverItems = FindMatches(
                item,
                _profile.BrokerResultServerItem);
            var serverNames = new List<string>();
            foreach (AutomationElement serverItem in serverItems)
            {
                AutomationElement[] serverLabels = FindMatches(
                    serverItem,
                    _profile.BrokerResultServerLabel);
                if (serverLabels.Length != 1
                    || string.IsNullOrWhiteSpace(serverLabels[0].Name))
                {
                    throw new InvalidOperationException(
                        "A broker result server did not expose exactly one safe label.");
                }

                serverNames.Add(serverLabels[0].Name);
            }

            candidates.Add(
                new BrokerWizardCandidate(stableId, labels[0].Name, serverNames));
        }

        return candidates;
    }

    public Task SelectBrokerAsync(
        BrokerWizardCandidate candidate,
        CancellationToken cancellationToken)
    {
        ArgumentNullException.ThrowIfNull(candidate);
        cancellationToken.ThrowIfCancellationRequested();
        if (!_candidateElements.TryGetValue(candidate.StableId, out AutomationElement? element))
        {
            throw new InvalidOperationException(
                "The broker candidate does not belong to the current result set.");
        }

        Select(element, "broker result");
        return Task.CompletedTask;
    }

    public async Task ConfirmBrokerSelectionAsync(
        CancellationToken cancellationToken)
    {
        AutomationElement wizard = RequireWizardWindow();
        AutomationElement confirm = await WaitForSingleAsync(
            () => FindMatches(wizard, _profile.ConfirmCommand),
            "broker confirmation command",
            cancellationToken).ConfigureAwait(false);
        Invoke(confirm, "broker confirmation command");
    }

    public async Task<IReadOnlyList<string>> ReadCensusedServerNamesAsync(
        CancellationToken cancellationToken)
    {
        AutomationElement mainWindow = RequireMainWindow();
        AutomationElement container = await WaitForSingleAsync(
            () => FindMatches(mainWindow, _profile.CensusedServerContainer),
            "censused server container",
            cancellationToken).ConfigureAwait(false);
        AutomationElement[] items = FindMatches(container, _profile.CensusedServerItem);

        var names = new List<string>();
        foreach (AutomationElement item in items)
        {
            AutomationElement[] labels = FindMatches(item, _profile.CensusedServerLabel);
            if (labels.Length != 1 || string.IsNullOrWhiteSpace(labels[0].Name))
            {
                throw new InvalidOperationException(
                    "A censused server did not expose exactly one safe label.");
            }

            names.Add(labels[0].Name);
        }

        return names;
    }

    public Task AbortAsync(CancellationToken cancellationToken)
    {
        cancellationToken.ThrowIfCancellationRequested();
        if (_wizardWindow is null || _profile.CancelCommand is null)
        {
            return Task.CompletedTask;
        }

        AutomationElement[] matches = FindMatches(
            _wizardWindow,
            _profile.CancelCommand);
        if (matches.Length == 1)
        {
            Invoke(matches[0], "broker wizard cancel command");
        }

        return Task.CompletedTask;
    }

    public void Dispose()
    {
        if (_disposed)
        {
            return;
        }

        _automation.Dispose();
        _application.Dispose();
        _disposed = true;
    }

    private static AutomationElement[] FindMatches(
        AutomationElement root,
        Mt5UiSelector selector) =>
        root.FindAllDescendants()
            .Where(element => Match(element, selector))
            .ToArray();

    private static bool Match(
        AutomationElement element,
        Mt5UiSelector selector)
    {
        if (!element.ControlType.ToString().Equals(
            selector.ControlType,
            StringComparison.OrdinalIgnoreCase))
        {
            return false;
        }

        if (selector.AutomationId is not null
            && !element.AutomationId.Equals(
                selector.AutomationId,
                StringComparison.Ordinal))
        {
            return false;
        }

        return selector.Name is null
            || element.Name.Equals(selector.Name, StringComparison.Ordinal);
    }

    private static void Invoke(AutomationElement element, string description)
    {
        if (!element.Patterns.Invoke.IsSupported)
        {
            throw new InvalidOperationException(
                $"{description} does not support the Invoke pattern.");
        }

        element.Patterns.Invoke.Pattern.Invoke();
    }

    private static void Select(AutomationElement element, string description)
    {
        if (element.Patterns.SelectionItem.IsSupported)
        {
            element.Patterns.SelectionItem.Pattern.Select();
            return;
        }

        if (element.Patterns.Invoke.IsSupported)
        {
            element.Patterns.Invoke.Pattern.Invoke();
            return;
        }

        throw new InvalidOperationException(
            $"{description} supports neither SelectionItem nor Invoke.");
    }

    private static void SetValue(
        AutomationElement element,
        string value,
        string description)
    {
        if (!element.Patterns.Value.IsSupported)
        {
            throw new InvalidOperationException(
                $"{description} does not support the Value pattern.");
        }

        element.Patterns.Value.Pattern.SetValue(value);
    }

    private static async Task<AutomationElement> WaitForSingleAsync(
        Func<AutomationElement[]> probe,
        string description,
        CancellationToken cancellationToken)
    {
        while (true)
        {
            cancellationToken.ThrowIfCancellationRequested();
            AutomationElement[] matches = probe();
            if (matches.Length == 1)
            {
                return matches[0];
            }

            if (matches.Length > 1)
            {
                throw new InvalidOperationException(
                    $"{description} selector is ambiguous.");
            }

            await Task.Delay(PollInterval, cancellationToken).ConfigureAwait(false);
        }
    }

    private AutomationElement RequireMainWindow()
    {
        ThrowIfDisposed();
        return _mainWindow
            ?? throw new InvalidOperationException(
                "WaitForTerminalAsync must complete first.");
    }

    private AutomationElement RequireWizardWindow()
    {
        ThrowIfDisposed();
        return _wizardWindow
            ?? throw new InvalidOperationException(
                "OpenFindBrokerAsync must complete first.");
    }

    private void ThrowIfDisposed()
    {
        ObjectDisposedException.ThrowIf(_disposed, this);
    }
}

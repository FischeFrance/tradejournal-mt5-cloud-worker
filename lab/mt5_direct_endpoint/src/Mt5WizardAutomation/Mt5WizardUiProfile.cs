namespace TradeJournal.Lab.Mt5WizardAutomation;

public sealed record Mt5UiSelector(
    string ControlType,
    string? AutomationId = null,
    string? Name = null);

public sealed record Mt5WizardUiProfile(
    string ProfileVersion,
    Mt5UiSelector MainWindow,
    Mt5UiSelector FindBrokerCommand,
    Mt5UiSelector WizardWindow,
    Mt5UiSelector BrokerSearchBox,
    Mt5UiSelector SearchCommand,
    Mt5UiSelector ResultsContainer,
    Mt5UiSelector BrokerResultItem,
    Mt5UiSelector BrokerResultLabel,
    Mt5UiSelector BrokerResultServerItem,
    Mt5UiSelector BrokerResultServerLabel,
    Mt5UiSelector ConfirmCommand,
    Mt5UiSelector CensusedServerContainer,
    Mt5UiSelector CensusedServerItem,
    Mt5UiSelector CensusedServerLabel,
    Mt5UiSelector? CancelCommand)
{
    private const int MaximumValueLength = 256;

    public void Validate()
    {
        if (!IsSafeValue(ProfileVersion))
        {
            throw new InvalidOperationException("The UI profile version is invalid.");
        }

        ValidateSingle(MainWindow, nameof(MainWindow));
        ValidateSingle(FindBrokerCommand, nameof(FindBrokerCommand));
        ValidateSingle(WizardWindow, nameof(WizardWindow));
        ValidateSingle(BrokerSearchBox, nameof(BrokerSearchBox));
        ValidateSingle(SearchCommand, nameof(SearchCommand));
        ValidateSingle(ResultsContainer, nameof(ResultsContainer));
        ValidateMany(BrokerResultItem, nameof(BrokerResultItem));
        ValidateSingle(BrokerResultLabel, nameof(BrokerResultLabel));
        ValidateMany(BrokerResultServerItem, nameof(BrokerResultServerItem));
        ValidateSingle(BrokerResultServerLabel, nameof(BrokerResultServerLabel));
        ValidateSingle(ConfirmCommand, nameof(ConfirmCommand));
        ValidateSingle(CensusedServerContainer, nameof(CensusedServerContainer));
        ValidateMany(CensusedServerItem, nameof(CensusedServerItem));
        ValidateSingle(CensusedServerLabel, nameof(CensusedServerLabel));
        if (CancelCommand is not null)
        {
            ValidateSingle(CancelCommand, nameof(CancelCommand));
        }
    }

    private static void ValidateSingle(Mt5UiSelector? selector, string field)
    {
        ValidateMany(selector, field);
        if (string.IsNullOrWhiteSpace(selector!.AutomationId)
            && string.IsNullOrWhiteSpace(selector.Name))
        {
            throw new InvalidOperationException(
                $"{field} must identify one control by AutomationId or Name.");
        }
    }

    private static void ValidateMany(Mt5UiSelector? selector, string field)
    {
        if (selector is null || !IsSafeValue(selector.ControlType))
        {
            throw new InvalidOperationException($"{field} has an invalid control type.");
        }

        if (selector.AutomationId is not null && !IsSafeValue(selector.AutomationId))
        {
            throw new InvalidOperationException($"{field} has an invalid AutomationId.");
        }

        if (selector.Name is not null && !IsSafeValue(selector.Name))
        {
            throw new InvalidOperationException($"{field} has an invalid Name.");
        }
    }

    private static bool IsSafeValue(string? value) =>
        !string.IsNullOrWhiteSpace(value)
        && value.Length <= MaximumValueLength
        && value.All(character => !char.IsControl(character));
}

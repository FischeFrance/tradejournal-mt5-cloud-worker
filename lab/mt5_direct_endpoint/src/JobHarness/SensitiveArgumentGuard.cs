using System.Text.RegularExpressions;

namespace TradeJournal.Lab.JobHarness;

internal static partial class SensitiveArgumentGuard
{
    // This is a guardrail, not a secret scanner. The runbook still forbids every credential
    // on a command line, including positional values that cannot be identified reliably.
    public static bool LooksSensitive(string argument)
    {
        ArgumentNullException.ThrowIfNull(argument);
        return SensitiveSwitch().IsMatch(argument) || EmbeddedAssignment().IsMatch(argument);
    }

    [GeneratedRegex(
        "^(?:--?|/)(?:password|passwd|pwd|token|secret|login|account)(?:$|[:=])",
        RegexOptions.IgnoreCase | RegexOptions.CultureInvariant)]
    private static partial Regex SensitiveSwitch();

    [GeneratedRegex(
        "(?:^|[;,&\\s])(?:password|passwd|pwd|token|secret|login|account)\\s*[:=]",
        RegexOptions.IgnoreCase | RegexOptions.CultureInvariant)]
    private static partial Regex EmbeddedAssignment();
}

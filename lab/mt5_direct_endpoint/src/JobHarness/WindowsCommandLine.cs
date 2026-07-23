using System.Text;

namespace TradeJournal.Lab.JobHarness;

internal static class WindowsCommandLine
{
    // Implements the CommandLineToArgvW/MSVC escaping convention used by most Windows apps.
    public static string Build(string executablePath, IReadOnlyList<string> arguments)
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(executablePath);
        ArgumentNullException.ThrowIfNull(arguments);

        var builder = new StringBuilder();
        AppendQuoted(builder, executablePath);
        foreach (string argument in arguments)
        {
            builder.Append(' ');
            AppendQuoted(builder, argument);
        }

        return builder.ToString();
    }

    internal static void AppendQuoted(StringBuilder builder, string value)
    {
        ArgumentNullException.ThrowIfNull(builder);
        ArgumentNullException.ThrowIfNull(value);

        builder.Append('"');
        int pendingBackslashes = 0;
        foreach (char character in value)
        {
            if (character == '\\')
            {
                pendingBackslashes++;
                continue;
            }

            if (character == '"')
            {
                builder.Append('\\', (pendingBackslashes * 2) + 1);
                builder.Append('"');
                pendingBackslashes = 0;
                continue;
            }

            builder.Append('\\', pendingBackslashes);
            pendingBackslashes = 0;
            builder.Append(character);
        }

        // Backslashes immediately before the closing quote must be doubled.
        builder.Append('\\', pendingBackslashes * 2);
        builder.Append('"');
    }
}

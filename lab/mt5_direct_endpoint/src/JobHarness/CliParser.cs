using System.Globalization;
using System.Text.RegularExpressions;

namespace TradeJournal.Lab.JobHarness;

internal sealed record LaunchOptions(
    string ExecutablePath,
    IReadOnlyList<string> Arguments,
    string WorkingDirectory,
    string? MetadataPath,
    string? ExpectedSha256,
    string RunId,
    string Phase,
    TimeSpan? Timeout,
    bool ExecuteRequested,
    bool DryRun,
    bool NoWindow,
    bool ConfirmMetaTraderLaunch,
    IReadOnlyList<string> AllowedSignerSubjects,
    IReadOnlyList<string> AllowedSignerThumbprints)
{
    public bool TargetLooksLikeMetaTrader
    {
        get
        {
            string fileName = Path.GetFileName(ExecutablePath);
            return fileName.Equals("terminal.exe", StringComparison.OrdinalIgnoreCase)
                || fileName.Equals("terminal64.exe", StringComparison.OrdinalIgnoreCase);
        }
    }
}

internal sealed record CliParseResult(LaunchOptions? Options, bool ShowHelp, string? Error);

internal static partial class CliParser
{
    internal const string HelpText = """
JobHarness - isolated Windows Job Object launcher for the MT5 endpoint laboratory

Usage:
  JobHarness --help
  JobHarness run --executable <absolute-path> [options]

Required for an actual launch:
  --executable <path>       Absolute path to the executable.
  --execute                 Explicitly opt in to process creation.
  --metadata <path>         New JSON evidence file; existing files are never overwritten.
  --expected-sha256 <hex>   Expected 64-hex SHA-256 digest of the leased executable.

Current safety gate:
  Actual launch is HARD_DISABLED pending Windows canonical-file, Authenticode and
  Job lifecycle validation. --execute validates/refuses and never creates a process.

Options:
  --arg <value>             One target argument. Repeat for multiple arguments.
  --working-directory <p>   Existing directory; defaults to the executable directory.
  --run-id <guid>           Correlation ID; generated when omitted.
  --phase <name>            Sanitized phase label (for example C0 or C3).
  --timeout-seconds <n>     Kill the complete job after 1..86400 seconds.
  --no-window               Add CREATE_NO_WINDOW (useful for an innocent cmd.exe smoke test).
  --confirm-mt5-launch      Required if the explicit target is terminal.exe/terminal64.exe.
  --allowed-signer-subject <distinguished-name>
                            Exact Authenticode signer subject allowed for an MT5-like target.
                            Repeat to allow more than one subject.
  --allowed-signer-thumbprint <hex>
                            Allowed signer certificate SHA-1 (40 hex) or SHA-256 (64 hex).
                            Repeat to allow more than one thumbprint.
  --dry-run                 Validate and emit metadata without creating a process or Job Object.

Security properties:
  * The target is always explicit; JobHarness never discovers or starts MT5 by itself.
  * No process is created unless --execute is present; --dry-run and --execute conflict.
  * Every actual launch is pinned to --expected-sha256 while the target lease is held.
  * MT5-like targets additionally require confirmation and trusted, allowlisted Authenticode.
  * Arguments and environment values are never written to JSON.
  * Arguments that look like passwords, login IDs, tokens or secrets are refused.
  * Dormant design: CreateProcessW would use CREATE_SUSPENDED before Job assignment/resume.
  * Dormant design: a future target would receive only the fixed environment allowlist.
  * Job containment and KILL_ON_JOB_CLOSE are not runtime-verified in this build.

Use a protected bootstrap configuration file for future credentials. Never put credentials
in --arg, the command line, the environment, the metadata filename, or the working directory.
""";

    private const int MaximumArgumentCount = 64;
    private const int MaximumArgumentLength = 4096;
    private const int MaximumWindowsCommandLineLength = 32766;
    private const int MaximumSignerAllowlistEntries = 16;
    private const int MaximumSignerSubjectLength = 512;

    public static CliParseResult Parse(string[] args)
    {
        if (args.Length == 0 || args is ["--help"] or ["-h"] or ["help"])
        {
            return new CliParseResult(null, true, null);
        }

        if (!args[0].Equals("run", StringComparison.OrdinalIgnoreCase))
        {
            return Error("Expected the 'run' command.");
        }

        string? executable = null;
        string? workingDirectory = null;
        string? metadata = null;
        string? expectedSha256 = null;
        string? suppliedRunId = null;
        string phase = "UNSPECIFIED";
        TimeSpan? timeout = null;
        bool execute = false;
        bool dryRun = false;
        bool noWindow = false;
        bool confirmMetaTrader = false;
        var targetArguments = new List<string>();
        var allowedSignerSubjects = new List<string>();
        var allowedSignerThumbprints = new List<string>();
        var seenSingleOptions = new HashSet<string>(StringComparer.Ordinal);

        for (int index = 1; index < args.Length; index++)
        {
            string option = args[index];
            if (IsSingleUseOption(option) && !seenSingleOptions.Add(option))
            {
                return Error($"Option at argument index {index} was repeated.");
            }

            switch (option)
            {
                case "--executable":
                    if (!TryReadValue(args, ref index, out executable))
                    {
                        return Error("--executable requires a value.");
                    }

                    break;
                case "--working-directory":
                    if (!TryReadValue(args, ref index, out workingDirectory))
                    {
                        return Error("--working-directory requires a value.");
                    }

                    break;
                case "--metadata":
                    if (!TryReadValue(args, ref index, out metadata))
                    {
                        return Error("--metadata requires a value.");
                    }

                    break;
                case "--expected-sha256":
                    if (!TryReadValue(args, ref index, out expectedSha256)
                        || !Sha256Pattern().IsMatch(expectedSha256))
                    {
                        return Error("--expected-sha256 must contain exactly 64 hexadecimal characters.");
                    }

                    expectedSha256 = expectedSha256.ToLowerInvariant();
                    break;
                case "--run-id":
                    if (!TryReadValue(args, ref index, out suppliedRunId))
                    {
                        return Error("--run-id requires a value.");
                    }

                    break;
                case "--phase":
                    if (!TryReadValue(args, ref index, out phase))
                    {
                        return Error("--phase requires a value.");
                    }

                    break;
                case "--timeout-seconds":
                    if (!TryReadValue(args, ref index, out string secondsText)
                        || !int.TryParse(secondsText, NumberStyles.None, CultureInfo.InvariantCulture, out int seconds)
                        || seconds is < 1 or > 86400)
                    {
                        return Error("--timeout-seconds must be an integer between 1 and 86400.");
                    }

                    timeout = TimeSpan.FromSeconds(seconds);
                    break;
                case "--arg":
                    if (!TryReadValue(args, ref index, out string targetArgument))
                    {
                        return Error("--arg requires a value.");
                    }

                    targetArguments.Add(targetArgument);
                    break;
                case "--execute":
                    execute = true;
                    break;
                case "--dry-run":
                    dryRun = true;
                    break;
                case "--no-window":
                    noWindow = true;
                    break;
                case "--confirm-mt5-launch":
                    confirmMetaTrader = true;
                    break;
                case "--allowed-signer-subject":
                    if (!TryReadValue(args, ref index, out string signerSubject)
                        || signerSubject.Length is < 1 or > MaximumSignerSubjectLength
                        || signerSubject.Any(char.IsControl))
                    {
                        return Error($"--allowed-signer-subject must contain 1..{MaximumSignerSubjectLength} printable characters.");
                    }

                    allowedSignerSubjects.Add(signerSubject);
                    break;
                case "--allowed-signer-thumbprint":
                    if (!TryReadValue(args, ref index, out string signerThumbprint)
                        || !AuthenticodeAllowlist.TryNormalizeThumbprint(signerThumbprint, out string normalizedThumbprint))
                    {
                        return Error("--allowed-signer-thumbprint must be a 40-hex SHA-1 or 64-hex SHA-256 certificate thumbprint.");
                    }

                    allowedSignerThumbprints.Add(normalizedThumbprint);
                    break;
                case "--help":
                case "-h":
                    return new CliParseResult(null, true, null);
                default:
                    return Error($"Unknown option at argument index {index}.");
            }
        }

        if (string.IsNullOrWhiteSpace(executable))
        {
            return Error("--executable is required.");
        }

        if (!Path.IsPathFullyQualified(executable))
        {
            return Error("--executable must be an absolute path.");
        }

        string fullExecutable;
        try
        {
            fullExecutable = Path.GetFullPath(executable);
        }
        catch (Exception exception) when (exception is ArgumentException or NotSupportedException or PathTooLongException)
        {
            return Error("--executable is not a valid path.");
        }

        if (!File.Exists(fullExecutable))
        {
            return Error("The target executable does not exist.");
        }

        string fullWorkingDirectory;
        try
        {
            fullWorkingDirectory = workingDirectory is null
                ? Path.GetDirectoryName(fullExecutable) ?? throw new InvalidOperationException()
                : Path.GetFullPath(workingDirectory);
        }
        catch (Exception exception) when (exception is ArgumentException or NotSupportedException or PathTooLongException or InvalidOperationException)
        {
            return Error("The working directory is not a valid path.");
        }

        if (!Path.IsPathFullyQualified(fullWorkingDirectory) || !Directory.Exists(fullWorkingDirectory))
        {
            return Error("The working directory must be an existing absolute path.");
        }

        string? fullMetadata = null;
        if (metadata is not null)
        {
            try
            {
                fullMetadata = Path.GetFullPath(metadata);
            }
            catch (Exception exception) when (exception is ArgumentException or NotSupportedException or PathTooLongException)
            {
                return Error("--metadata is not a valid path.");
            }

            if (PathEquals(fullMetadata, fullExecutable))
            {
                return Error("The metadata path cannot be the executable path.");
            }
        }

        if (targetArguments.Count > MaximumArgumentCount)
        {
            return Error($"No more than {MaximumArgumentCount} target arguments are allowed.");
        }

        if (allowedSignerSubjects.Count + allowedSignerThumbprints.Count > MaximumSignerAllowlistEntries)
        {
            return Error($"No more than {MaximumSignerAllowlistEntries} signer allowlist entries are allowed.");
        }

        for (int index = 0; index < targetArguments.Count; index++)
        {
            string targetArgument = targetArguments[index];
            if (targetArgument.Length > MaximumArgumentLength)
            {
                return Error($"Target argument {index} exceeds {MaximumArgumentLength} characters.");
            }

            if (SensitiveArgumentGuard.LooksSensitive(targetArgument))
            {
                return Error($"Target argument {index} looks sensitive and was refused.");
            }
        }

        string commandLine = WindowsCommandLine.Build(fullExecutable, targetArguments);
        if (commandLine.Length > MaximumWindowsCommandLineLength)
        {
            return Error("The target command line is too long for CreateProcessW.");
        }

        if (!PhasePattern().IsMatch(phase))
        {
            return Error("--phase must contain 1..32 letters, digits, dot, underscore or hyphen.");
        }

        string runId;
        if (suppliedRunId is null)
        {
            runId = Guid.NewGuid().ToString("D");
        }
        else if (!Guid.TryParseExact(suppliedRunId, "D", out Guid parsedRunId))
        {
            return Error("--run-id must be a canonical GUID (xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx).");
        }
        else
        {
            runId = parsedRunId.ToString("D");
        }

        return new CliParseResult(
            new LaunchOptions(
                fullExecutable,
                targetArguments.AsReadOnly(),
                fullWorkingDirectory,
                fullMetadata,
                expectedSha256,
                runId,
                phase,
                timeout,
                execute,
                dryRun,
                noWindow,
                confirmMetaTrader,
                allowedSignerSubjects.AsReadOnly(),
                allowedSignerThumbprints.AsReadOnly()),
            false,
            null);
    }

    private static bool TryReadValue(string[] args, ref int index, out string value)
    {
        if (index + 1 >= args.Length)
        {
            value = string.Empty;
            return false;
        }

        value = args[++index];
        return true;
    }

    private static bool IsSingleUseOption(string option) => option is
        "--executable"
        or "--working-directory"
        or "--metadata"
        or "--expected-sha256"
        or "--run-id"
        or "--phase"
        or "--timeout-seconds"
        or "--execute"
        or "--dry-run"
        or "--no-window"
        or "--confirm-mt5-launch";

    private static bool PathEquals(string left, string right) =>
        string.Equals(
            Path.TrimEndingDirectorySeparator(left),
            Path.TrimEndingDirectorySeparator(right),
            OperatingSystem.IsWindows() ? StringComparison.OrdinalIgnoreCase : StringComparison.Ordinal);

    private static CliParseResult Error(string message) => new(null, false, message);

    [GeneratedRegex("^[A-Za-z0-9_.-]{1,32}$", RegexOptions.CultureInvariant)]
    private static partial Regex PhasePattern();

    [GeneratedRegex("^[A-Fa-f0-9]{64}$", RegexOptions.CultureInvariant)]
    private static partial Regex Sha256Pattern();
}

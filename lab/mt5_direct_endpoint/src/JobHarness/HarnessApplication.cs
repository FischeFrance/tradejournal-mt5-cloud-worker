using System.ComponentModel;

namespace TradeJournal.Lab.JobHarness;

internal delegate AuthenticodeVerification AuthenticodeVerifierDelegate(
    string executablePath,
    AuthenticodeAllowlist allowlist);

internal delegate JobRunResult JobRunnerDelegate(
    LaunchOptions options,
    HarnessMetadata metadata,
    MetadataWriter writer);

internal sealed record HarnessRuntime(
    Func<bool> IsWindows,
    Func<string?> GetCurrentUserSid,
    AuthenticodeVerifierDelegate VerifyAuthenticode,
    JobRunnerDelegate RunJob)
{
    public static HarnessRuntime System { get; } = new(
        OperatingSystem.IsWindows,
        ExecutionIdentity.GetCurrentUserSid,
        AuthenticodeVerifier.Verify,
        JobObjectRunner.Run);
}

internal static class HarnessApplication
{
    internal const int ExitUsage = 2;
    internal const int ExitLaunchFailure = 125;

    // The current build has not yet proven canonical file identity, created-image
    // identity, Authenticode signer/provider binding, or Job close semantics on
    // Windows. Keep every real launch fail-closed until that validation lands.
    private static bool ActualLaunchRuntimeValidated => false;

    public static int Execute(string[] args, TextWriter output, TextWriter error) =>
        Execute(args, output, error, HarnessRuntime.System);

    internal static int Execute(
        string[] args,
        TextWriter output,
        TextWriter error,
        HarnessRuntime runtime)
    {
        ArgumentNullException.ThrowIfNull(args);
        ArgumentNullException.ThrowIfNull(output);
        ArgumentNullException.ThrowIfNull(error);
        ArgumentNullException.ThrowIfNull(runtime);

        CliParseResult parsed = CliParser.Parse(args);
        if (parsed.ShowHelp)
        {
            output.WriteLine(CliParser.HelpText);
            return 0;
        }

        if (parsed.Error is not null || parsed.Options is null)
        {
            error.WriteLine(parsed.Error ?? "Invalid command line.");
            error.WriteLine("Use 'JobHarness --help' for usage.");
            return ExitUsage;
        }

        LaunchOptions options = parsed.Options;
        HarnessMetadata metadata;
        TargetExecutableLease targetLease;
        try
        {
            targetLease = TargetExecutableLease.Open(options.ExecutablePath);
            string? userSid = runtime.IsWindows() ? runtime.GetCurrentUserSid() : null;
            metadata = HarnessMetadataFactory.Create(options, targetLease, userSid);
        }
        catch (Exception exception) when (exception is IOException or UnauthorizedAccessException)
        {
            error.WriteLine("Unable to inspect the target executable.");
            return ExitUsage;
        }

        using (targetLease)
        {
            MetadataWriter? writer = null;
            try
            {
                if (options.MetadataPath is not null)
                {
                    writer = new MetadataWriter(options.MetadataPath);
                }

                if (options.ExecuteRequested && options.DryRun)
                {
                    return Refuse(
                        metadata,
                        writer,
                        error,
                        "execute_dry_run_conflict",
                        "Launch refused: --execute and --dry-run are mutually exclusive.");
                }

                if (options.DryRun)
                {
                    metadata.Status = HarnessStatus.DryRun;
                    metadata.CompletedUtc = DateTimeOffset.UtcNow;
                    if (writer is null)
                    {
                        output.WriteLine(MetadataWriter.Serialize(metadata));
                    }
                    else
                    {
                        writer.Write(metadata);
                        output.WriteLine($"JobHarness dry-run complete; run_id={metadata.RunId}; metadata written.");
                    }

                    return 0;
                }

                if (!options.ExecuteRequested)
                {
                    return Refuse(
                        metadata,
                        writer,
                        error,
                        "execute_not_requested",
                        "Launch refused: add --execute only for a separately authorized actual launch, or use --dry-run.");
                }

                if (writer is null)
                {
                    return Refuse(
                        metadata,
                        writer,
                        error,
                        "metadata_required",
                        "Launch refused: --metadata is required for an actual launch.");
                }

                if (options.ExpectedSha256 is null)
                {
                    return Refuse(
                        metadata,
                        writer,
                        error,
                        "expected_sha256_required",
                        "Launch refused: --expected-sha256 is required for an actual launch.");
                }

                if (metadata.Target.Sha256Match != true)
                {
                    return Refuse(
                        metadata,
                        writer,
                        error,
                        "executable_sha256_mismatch",
                        "Launch refused: the leased executable does not match --expected-sha256.");
                }

                if (options.TargetLooksLikeMetaTrader && !options.ConfirmMetaTraderLaunch)
                {
                    return Refuse(
                        metadata,
                        writer,
                        error,
                        "mt5_confirmation_required",
                        "MetaTrader launch refused: --confirm-mt5-launch is required in addition to --execute.");
                }

                if (!runtime.IsWindows())
                {
                    metadata.Status = HarnessStatus.PlatformUnsupported;
                    metadata.CompletedUtc = DateTimeOffset.UtcNow;
                    metadata.Error = new SanitizedError("platform", null);
                    writer.Write(metadata);
                    error.WriteLine("Actual launches are supported only on Windows. Use --dry-run for validation.");
                    return ExitUsage;
                }

                if (metadata.Process.UserSid is null)
                {
                    return Refuse(
                        metadata,
                        writer,
                        error,
                        "windows_user_sid_unavailable",
                        "Launch refused: the Windows user SID could not be recorded.");
                }

                if (options.TargetLooksLikeMetaTrader)
                {
                    var allowlist = new AuthenticodeAllowlist(
                        options.AllowedSignerSubjects,
                        options.AllowedSignerThumbprints);
                    if (!allowlist.HasEntries)
                    {
                        return Refuse(
                            metadata,
                            writer,
                            error,
                            "authenticode_allowlist_required",
                            "MetaTrader launch refused: configure an exact signer subject or certificate thumbprint allowlist.");
                    }

                    AuthenticodeVerification verification;
                    try
                    {
                        verification = runtime.VerifyAuthenticode(options.ExecutablePath, allowlist);
                    }
                    catch (Exception)
                    {
                        metadata.Target.Authenticode.VerificationAttempted = true;
                        metadata.Target.Authenticode.VerificationMode = "VERIFICATION_ERROR";
                        metadata.Target.Authenticode.LaunchApproved = false;
                        return Refuse(
                            metadata,
                            writer,
                            error,
                            "authenticode_verification_error",
                            "MetaTrader launch refused: Authenticode verification could not be completed safely.");
                    }

                    metadata.Target.Authenticode.Apply(verification);
                    if (!verification.PreliminaryChecksPassed)
                    {
                        string category = !verification.SignaturePresent
                            ? "authenticode_signature_missing"
                            : !verification.TrustValid
                                ? "authenticode_trust_invalid"
                                : "authenticode_signer_not_allowlisted";
                        return Refuse(
                            metadata,
                            writer,
                            error,
                            category,
                            "MetaTrader launch refused: Authenticode trust and signer allowlist checks did not pass.");
                    }
                }

                if (!ActualLaunchRuntimeValidated)
                {
                    metadata.Target.Authenticode.LaunchApproved = false;
                    return Refuse(
                        metadata,
                        writer,
                        error,
                        "actual_launch_runtime_validation_required",
                        "Launch refused: canonical file identity, created-image binding, Authenticode and Job lifecycle still require Windows runtime validation.");
                }

                metadata.Status = HarnessStatus.Validated;
                writer.Write(metadata);

                JobRunResult result = runtime.RunJob(options, metadata, writer);
                output.WriteLine($"JobHarness finished; run_id={metadata.RunId}; status={metadata.Status}; metadata written.");
                return result.ExitCode;
            }
            catch (Exception exception) when (exception is Win32Exception or IOException or UnauthorizedAccessException or InvalidOperationException)
            {
                metadata.Status = HarnessStatus.Failed;
                metadata.CompletedUtc = DateTimeOffset.UtcNow;
                metadata.Error = SanitizedError.FromException(exception);
                writer?.TryWrite(metadata);
                error.WriteLine($"JobHarness failed safely; run_id={metadata.RunId}; category={metadata.Error.Category}.");
                return ExitLaunchFailure;
            }
        }
    }

    private static int Refuse(
        HarnessMetadata metadata,
        MetadataWriter? writer,
        TextWriter error,
        string category,
        string message)
    {
        metadata.Status = HarnessStatus.Refused;
        metadata.CompletedUtc = DateTimeOffset.UtcNow;
        metadata.Error = new SanitizedError(category, null);
        writer?.Write(metadata);
        error.WriteLine(message);
        return ExitUsage;
    }
}

using System.Text.Json.Serialization;

namespace TradeJournal.Lab.JobHarness;

internal static class HarnessStatus
{
    public const string Prepared = "PREPARED";
    public const string Validated = "VALIDATED";
    public const string DryRun = "DRY_RUN";
    public const string AssignedSuspended = "ASSIGNED_SUSPENDED";
    public const string Running = "RUNNING";
    public const string Completed = "COMPLETED";
    public const string TimedOut = "TIMED_OUT";
    public const string Cancelled = "CANCELLED";
    public const string Failed = "FAILED";
    public const string Refused = "REFUSED";
    public const string PlatformUnsupported = "PLATFORM_UNSUPPORTED";
}

internal sealed class HarnessMetadata
{
    [JsonPropertyName("schema_version")]
    public string SchemaVersion { get; init; } = "jobharness.process-metadata.v2";

    [JsonPropertyName("run_id")]
    public required string RunId { get; init; }

    [JsonPropertyName("phase")]
    public required string Phase { get; init; }

    [JsonPropertyName("status")]
    public required string Status { get; set; }

    [JsonPropertyName("prepared_utc")]
    public required DateTimeOffset PreparedUtc { get; init; }

    [JsonPropertyName("process_created_utc")]
    public DateTimeOffset? ProcessCreatedUtc { get; set; }

    [JsonPropertyName("assigned_utc")]
    public DateTimeOffset? AssignedUtc { get; set; }

    [JsonPropertyName("resumed_utc")]
    public DateTimeOffset? ResumedUtc { get; set; }

    [JsonPropertyName("completed_utc")]
    public DateTimeOffset? CompletedUtc { get; set; }

    [JsonPropertyName("target")]
    public required TargetMetadata Target { get; init; }

    [JsonPropertyName("launch_policy")]
    public required LaunchPolicyMetadata LaunchPolicy { get; init; }

    [JsonPropertyName("process")]
    public ProcessMetadata Process { get; init; } = new();

    [JsonPropertyName("job_policy")]
    public required JobPolicyMetadata JobPolicy { get; init; }

    [JsonPropertyName("evidence_hygiene")]
    public EvidenceHygieneMetadata EvidenceHygiene { get; init; } = new();

    [JsonPropertyName("result")]
    public ProcessResultMetadata ProcessResult { get; init; } = new();

    [JsonPropertyName("error")]
    public SanitizedError? Error { get; set; }
}

internal sealed record TargetMetadata(
    [property: JsonPropertyName("executable_path")] string ExecutablePath,
    [property: JsonPropertyName("canonical_path_verified")] bool CanonicalPathVerified,
    [property: JsonPropertyName("file_identity_verified")] bool FileIdentityVerified,
    [property: JsonPropertyName("executable_file_name")] string ExecutableFileName,
    [property: JsonPropertyName("expected_sha256")] string? ExpectedSha256,
    [property: JsonPropertyName("observed_sha256")] string ObservedSha256,
    [property: JsonPropertyName("sha256_match")] bool? Sha256Match,
    [property: JsonPropertyName("size_bytes")] long SizeBytes,
    [property: JsonPropertyName("last_write_utc")] DateTimeOffset LastWriteUtc,
    [property: JsonPropertyName("argument_count")] int ArgumentCount,
    [property: JsonPropertyName("looks_like_metatrader")] bool LooksLikeMetaTrader,
    [property: JsonPropertyName("authenticode")] AuthenticodeMetadata Authenticode);

internal sealed class LaunchPolicyMetadata
{
    [JsonPropertyName("execute_requested")]
    public bool ExecuteRequested { get; init; }

    [JsonPropertyName("dry_run_requested")]
    public bool DryRunRequested { get; init; }

    [JsonPropertyName("expected_sha256_required_for_execute")]
    public bool ExpectedSha256RequiredForExecute { get; init; } = true;

    [JsonPropertyName("metadata_required_for_execute")]
    public bool MetadataRequiredForExecute { get; init; } = true;

    [JsonPropertyName("actual_launch_capability")]
    public string ActualLaunchCapability { get; init; } = "HARD_DISABLED";

    [JsonPropertyName("windows_runtime_validation_complete")]
    public bool WindowsRuntimeValidationComplete { get; init; } = false;

    [JsonPropertyName("mt5_confirmation_required")]
    public bool MetaTraderConfirmationRequired { get; init; }

    [JsonPropertyName("mt5_confirmation_provided")]
    public bool MetaTraderConfirmationProvided { get; init; }
}

internal sealed class AuthenticodeMetadata
{
    [JsonPropertyName("required_for_launch")]
    public bool RequiredForLaunch { get; init; }

    [JsonPropertyName("allowlist_configured")]
    public bool AllowlistConfigured { get; init; }

    [JsonPropertyName("verification_attempted")]
    public bool VerificationAttempted { get; set; }

    [JsonPropertyName("verification_mode")]
    public string VerificationMode { get; set; } = "NOT_EVALUATED";

    [JsonPropertyName("signature_present")]
    public bool? SignaturePresent { get; set; }

    [JsonPropertyName("trust_valid")]
    public bool? TrustValid { get; set; }

    [JsonPropertyName("trust_provider_result")]
    public int? TrustProviderResult { get; set; }

    [JsonPropertyName("signer_subject")]
    public string? SignerSubject { get; set; }

    [JsonPropertyName("signer_thumbprint_sha1")]
    public string? SignerThumbprintSha1 { get; set; }

    [JsonPropertyName("signer_thumbprint_sha256")]
    public string? SignerThumbprintSha256 { get; set; }

    [JsonPropertyName("allowlist_match")]
    public bool? AllowlistMatch { get; set; }

    [JsonPropertyName("preliminary_checks_passed")]
    public bool? PreliminaryChecksPassed { get; set; }

    [JsonPropertyName("provider_signer_binding_verified")]
    public bool ProviderSignerBindingVerified { get; init; } = false;

    [JsonPropertyName("windows_runtime_validation_complete")]
    public bool WindowsRuntimeValidationComplete { get; init; } = false;

    [JsonPropertyName("launch_approved")]
    public bool LaunchApproved { get; set; } = false;

    public void Apply(AuthenticodeVerification verification)
    {
        ArgumentNullException.ThrowIfNull(verification);
        VerificationAttempted = verification.Attempted;
        VerificationMode = verification.VerificationMode;
        SignaturePresent = verification.SignaturePresent;
        TrustValid = verification.TrustValid;
        TrustProviderResult = verification.TrustProviderResult;
        SignerSubject = verification.SignerSubject;
        SignerThumbprintSha1 = verification.SignerThumbprintSha1;
        SignerThumbprintSha256 = verification.SignerThumbprintSha256;
        AllowlistMatch = verification.AllowlistMatched;
        PreliminaryChecksPassed = verification.PreliminaryChecksPassed;
        LaunchApproved = false;
    }
}

internal sealed class ProcessMetadata
{
    [JsonPropertyName("launcher_pid")]
    public int LauncherPid { get; init; } = Environment.ProcessId;

    [JsonPropertyName("user_sid")]
    public string? UserSid { get; init; }

    [JsonPropertyName("root_pid")]
    public uint? RootPid { get; set; }

    [JsonPropertyName("primary_thread_id")]
    public uint? PrimaryThreadId { get; set; }

    [JsonPropertyName("windows_session_id")]
    public uint? WindowsSessionId { get; set; }

    [JsonPropertyName("kernel_creation_utc")]
    public DateTimeOffset? KernelCreationUtc { get; set; }

    [JsonPropertyName("created_suspended")]
    public bool CreatedSuspended { get; set; }

    [JsonPropertyName("assigned_before_resume")]
    public bool AssignedBeforeResume { get; set; }

    [JsonPropertyName("resume_previous_suspend_count")]
    public uint? ResumePreviousSuspendCount { get; set; }
}

internal sealed class JobPolicyMetadata
{
    [JsonPropertyName("job_id")]
    public required string JobId { get; init; }

    [JsonPropertyName("unnamed_job")]
    public bool UnnamedJob { get; init; } = true;

    [JsonPropertyName("kill_on_job_close_requested")]
    public bool KillOnJobCloseRequested { get; init; } = true;

    [JsonPropertyName("kill_on_job_close_verified")]
    public bool? KillOnJobCloseVerified { get; set; }

    [JsonPropertyName("breakaway_allowed")]
    public bool? BreakawayAllowed { get; set; }

    [JsonPropertyName("silent_breakaway_allowed")]
    public bool? SilentBreakawayAllowed { get; set; }

    [JsonPropertyName("active_process_limit")]
    public uint? ActiveProcessLimit { get; init; }

    [JsonPropertyName("timeout_seconds")]
    public int? TimeoutSeconds { get; init; }

    [JsonPropertyName("environment_policy")]
    public required string EnvironmentPolicy { get; init; }

    [JsonPropertyName("create_no_window")]
    public bool CreateNoWindow { get; init; }
}

internal sealed class EvidenceHygieneMetadata
{
    [JsonPropertyName("arguments_recorded")]
    public bool ArgumentsRecorded { get; init; } = false;

    [JsonPropertyName("environment_values_recorded")]
    public bool EnvironmentValuesRecorded { get; init; } = false;

    [JsonPropertyName("working_directory_recorded")]
    public bool WorkingDirectoryRecorded { get; init; } = false;

    [JsonPropertyName("command_line_secret_guard_enabled")]
    public bool CommandLineSecretGuardEnabled { get; init; } = true;

    [JsonPropertyName("raw_output_captured")]
    public bool RawOutputCaptured { get; init; } = false;
}

internal sealed class ProcessResultMetadata
{
    [JsonPropertyName("primary_exit_code")]
    public uint? PrimaryExitCode { get; set; }

    [JsonPropertyName("job_total_processes")]
    public uint? JobTotalProcesses { get; set; }

    [JsonPropertyName("job_total_terminated_processes")]
    public uint? JobTotalTerminatedProcesses { get; set; }

    [JsonPropertyName("timed_out")]
    public bool TimedOut { get; set; }

    [JsonPropertyName("cancelled")]
    public bool Cancelled { get; set; }
}

internal sealed record SanitizedError(
    [property: JsonPropertyName("category")] string Category,
    [property: JsonPropertyName("native_error_code")] int? NativeErrorCode)
{
    public static SanitizedError FromException(Exception exception)
    {
        ArgumentNullException.ThrowIfNull(exception);
        return exception switch
        {
            System.ComponentModel.Win32Exception win32 => new("win32", win32.NativeErrorCode),
            UnauthorizedAccessException => new("access_denied", null),
            IOException => new("io", null),
            InvalidOperationException => new("invalid_operation", null),
            _ => new("unexpected", null),
        };
    }
}

internal static class HarnessMetadataFactory
{
    public static HarnessMetadata Create(
        LaunchOptions options,
        TargetExecutableLease targetLease,
        string? userSid)
    {
        ArgumentNullException.ThrowIfNull(options);
        ArgumentNullException.ThrowIfNull(targetLease);

        return new HarnessMetadata
        {
            RunId = options.RunId,
            Phase = options.Phase,
            Status = HarnessStatus.Prepared,
            PreparedUtc = DateTimeOffset.UtcNow,
            Target = new TargetMetadata(
                options.ExecutablePath,
                false,
                false,
                targetLease.FileName,
                options.ExpectedSha256,
                targetLease.Sha256,
                options.ExpectedSha256 is null
                    ? null
                    : targetLease.MatchesExpectedSha256(options.ExpectedSha256),
                targetLease.SizeBytes,
                targetLease.LastWriteUtc,
                options.Arguments.Count,
                options.TargetLooksLikeMetaTrader,
                new AuthenticodeMetadata
                {
                    RequiredForLaunch = options.ExecuteRequested && options.TargetLooksLikeMetaTrader,
                    AllowlistConfigured = options.AllowedSignerSubjects.Count > 0
                        || options.AllowedSignerThumbprints.Count > 0,
                }),
            LaunchPolicy = new LaunchPolicyMetadata
            {
                ExecuteRequested = options.ExecuteRequested,
                DryRunRequested = options.DryRun,
                MetaTraderConfirmationRequired = options.ExecuteRequested && options.TargetLooksLikeMetaTrader,
                MetaTraderConfirmationProvided = options.ConfirmMetaTraderLaunch,
            },
            Process = new ProcessMetadata
            {
                UserSid = userSid,
            },
            JobPolicy = new JobPolicyMetadata
            {
                JobId = Guid.NewGuid().ToString("D"),
                TimeoutSeconds = options.Timeout is null ? null : checked((int)options.Timeout.Value.TotalSeconds),
                EnvironmentPolicy = "ALLOWLIST",
                CreateNoWindow = options.NoWindow,
            },
        };
    }
}

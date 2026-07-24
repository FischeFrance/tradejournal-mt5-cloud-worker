using System.Diagnostics;
using System.ComponentModel;
using System.Reflection;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using TradeJournal.Lab.JobHarness;

if (args is ["--innocent-sleeper"])
{
    Thread.Sleep(TimeSpan.FromSeconds(30));
    return 0;
}

if (args is ["--innocent-process-tree", var childReadyPath])
{
    using Process child = Process.Start(CreateSelfStartInfo("--innocent-child", childReadyPath))
        ?? throw new InvalidOperationException("Unable to start the innocent child.");
    Thread.Sleep(TimeSpan.FromSeconds(30));
    return 0;
}

if (args is ["--innocent-child", var readyPath])
{
    using Process current = Process.GetCurrentProcess();
    File.WriteAllText(
        readyPath,
        $"{Environment.ProcessId}|{current.StartTime.ToUniversalTime().Ticks}",
        Encoding.UTF8);
    Thread.Sleep(TimeSpan.FromSeconds(30));
    return 0;
}

var tests = new (string Name, Action Body)[]
{
    ("no_flags_does_not_launch", NoFlagsDoesNotLaunch),
    ("parser_requires_absolute_existing_executable", ParserRequiresAbsoluteExistingExecutable),
    ("parser_rejects_sensitive_switches", ParserRejectsSensitiveSwitches),
    ("parser_rejects_environment_inheritance", ParserRejectsEnvironmentInheritance),
    ("parser_validates_expected_sha256", ParserValidatesExpectedSha256),
    ("windows_argument_quoting", WindowsArgumentQuoting),
    ("missing_execute_refused_with_sanitized_metadata", MissingExecuteRefusedWithSanitizedMetadata),
    ("execute_and_dry_run_are_mutually_exclusive", ExecuteAndDryRunAreMutuallyExclusive),
    ("execute_requires_metadata", ExecuteRequiresMetadata),
    ("execute_requires_expected_sha256", ExecuteRequiresExpectedSha256),
    ("wrong_hash_refused_without_launch", WrongHashRefusedWithoutLaunch),
    ("unsupported_platform_refused_without_launch", UnsupportedPlatformRefusedWithoutLaunch),
    ("correct_hash_still_requires_windows_runtime_validation", CorrectHashStillRequiresWindowsRuntimeValidation),
    ("mt5_like_target_requires_confirmation", MetaTraderLikeTargetRequiresConfirmation),
    ("mt5_like_target_requires_signer_allowlist", MetaTraderLikeTargetRequiresSignerAllowlist),
    ("mt5_like_target_invalid_authenticode_is_fail_closed", MetaTraderLikeTargetInvalidAuthenticodeIsFailClosed),
    ("mt5_like_target_trusted_allowlisted_remains_hard_disabled", MetaTraderLikeTargetTrustedAllowlistedRemainsHardDisabled),
    ("authenticode_allowlist_is_exact", AuthenticodeAllowlistIsExact),
    ("dry_run_metadata_omits_argument_values", DryRunMetadataOmitsArgumentValues),
    ("metadata_writer_refuses_existing_file", MetadataWriterRefusesExistingFile),
    ("metadata_path_policy_flags_unc", MetadataPathPolicyFlagsUnc),
    ("target_lease_matches_digest", TargetLeaseMatchesDigest),
    ("target_lease_blocks_write_on_windows", TargetLeaseBlocksWriteOnWindows),
    ("optional_windows_actual_launch_remains_hard_disabled", OptionalWindowsActualLaunchRemainsHardDisabled),
    ("optional_windows_descendant_smoke_remains_hard_disabled", OptionalWindowsDescendantSmokeRemainsHardDisabled),
    ("optional_windows_jobobject_natural_exit_runtime_smoke", OptionalWindowsJobObjectNaturalExitRuntimeSmoke),
    ("optional_windows_jobobject_descendant_timeout_runtime_smoke", OptionalWindowsJobObjectDescendantTimeoutRuntimeSmoke),
};

int failures = 0;
foreach ((string name, Action body) in tests)
{
    try
    {
        body();
        Console.WriteLine($"PASS {name}");
    }
    catch (Exception exception)
    {
        failures++;
        Console.Error.WriteLine($"FAIL {name}: {exception.GetType().Name}");
    }
}

return failures == 0 ? 0 : 1;

static void NoFlagsDoesNotLaunch()
{
    var launchProbe = new LaunchProbe();
    using var stdout = new StringWriter();
    using var stderr = new StringWriter();
    int exitCode = HarnessApplication.Execute([], stdout, stderr, CreateFakeWindowsRuntime(launchProbe));
    Assert(exitCode == 0, "help exit code");
    Assert(stdout.ToString().Contains("--execute", StringComparison.Ordinal), "help text");
    Assert(stderr.ToString().Length == 0, "help stderr");
    Assert(launchProbe.Calls == 0, "no flags must never reach the launch boundary");
}

static void ParserRequiresAbsoluteExistingExecutable()
{
    CliParseResult result = CliParser.Parse(["run", "--executable", "relative.exe", "--dry-run"]);
    Assert(result.Options is null, "relative executable should fail");
    Assert(result.Error?.Contains("absolute", StringComparison.OrdinalIgnoreCase) == true, "safe parser error");
}

static void ParserRejectsSensitiveSwitches()
{
    string executable = RequireCurrentExecutable();
    foreach (string sensitive in new[] { "--password=value", "/login:123456", "token=abc", "--secret" })
    {
        CliParseResult result = CliParser.Parse(
            ["run", "--executable", executable, "--dry-run", "--arg", sensitive]);
        Assert(result.Options is null, "sensitive argument should fail");
        Assert(!result.Error!.Contains(sensitive, StringComparison.Ordinal), "secret-like value echoed in error");
    }
}

static void ParserRejectsEnvironmentInheritance()
{
    string executable = RequireCurrentExecutable();
    CliParseResult result = CliParser.Parse(
        ["run", "--executable", executable, "--dry-run", "--inherit-environment"]);
    Assert(result.Options is null, "removed environment inheritance option should fail");
    Assert(result.Error?.Contains("Unknown option", StringComparison.Ordinal) == true, "safe unknown-option error");
}

static void ParserValidatesExpectedSha256()
{
    string executable = RequireCurrentExecutable();
    CliParseResult invalid = CliParser.Parse(
        ["run", "--executable", executable, "--execute", "--expected-sha256", "not-a-digest"]);
    Assert(invalid.Options is null, "invalid SHA-256 should fail parsing");

    string uppercase = new('A', 64);
    CliParseResult valid = CliParser.Parse(
        ["run", "--executable", executable, "--execute", "--expected-sha256", uppercase]);
    Assert(valid.Options?.ExpectedSha256 == uppercase.ToLowerInvariant(), "SHA-256 normalization");
}

static void WindowsArgumentQuoting()
{
    string commandLine = WindowsCommandLine.Build(
        @"C:\Program Files\Lab\probe.exe",
        ["plain", "two words", @"trailing\", "quote\"inside", string.Empty]);

    const string expected =
        "\"C:\\Program Files\\Lab\\probe.exe\" \"plain\" \"two words\" \"trailing\\\\\" \"quote\\\"inside\" \"\"";
    Assert(commandLine == expected, "quoted command line mismatch");
}

static void MissingExecuteRefusedWithSanitizedMetadata()
{
    WithTemporaryDirectory(directory =>
    {
        string metadataPath = Path.Combine(directory, "missing-execute.json");
        const string marker = "SAFE_ARGUMENT_MARKER_MUST_NOT_APPEAR";
        var launchProbe = new LaunchProbe();
        int exitCode = RunHarness(
            [
                "run",
                "--executable", RequireCurrentExecutable(),
                "--metadata", metadataPath,
                "--arg", marker,
            ],
            CreateFakeWindowsRuntime(launchProbe));

        Assert(exitCode == HarnessApplication.ExitUsage, "missing execute exit code");
        Assert(launchProbe.Calls == 0, "missing execute launch count");
        string json = File.ReadAllText(metadataPath, Encoding.UTF8);
        Assert(!json.Contains(marker, StringComparison.Ordinal), "refusal leaked argument value");
        using JsonDocument document = JsonDocument.Parse(json);
        AssertRefusal(document, "execute_not_requested");
        JsonElement root = document.RootElement;
        Assert(root.GetProperty("schema_version").GetString() == "jobharness.process-metadata.v2", "metadata schema");
        Assert(!root.GetProperty("launch_policy").GetProperty("execute_requested").GetBoolean(), "execute request evidence");
        Assert(Guid.TryParse(root.GetProperty("job_policy").GetProperty("job_id").GetString(), out _), "opaque job ID");
    });
}

static void ExecuteAndDryRunAreMutuallyExclusive()
{
    WithTemporaryDirectory(directory =>
    {
        string metadataPath = Path.Combine(directory, "conflict.json");
        var launchProbe = new LaunchProbe();
        int exitCode = RunHarness(
            [
                "run",
                "--executable", RequireCurrentExecutable(),
                "--metadata", metadataPath,
                "--execute",
                "--dry-run",
            ],
            CreateFakeWindowsRuntime(launchProbe));

        Assert(exitCode == HarnessApplication.ExitUsage, "execute/dry-run conflict exit code");
        Assert(launchProbe.Calls == 0, "execute/dry-run conflict launch count");
        using JsonDocument document = ReadJson(metadataPath);
        AssertRefusal(document, "execute_dry_run_conflict");
    });
}

static void ExecuteRequiresMetadata()
{
    var launchProbe = new LaunchProbe();
    string executable = RequireCurrentExecutable();
    int exitCode = RunHarness(
        [
            "run",
            "--executable", executable,
            "--execute",
            "--expected-sha256", ComputeSha256(executable),
        ],
        CreateFakeWindowsRuntime(launchProbe));

    Assert(exitCode == HarnessApplication.ExitUsage, "metadata required exit code");
    Assert(launchProbe.Calls == 0, "metadata required launch count");
}

static void ExecuteRequiresExpectedSha256()
{
    WithTemporaryDirectory(directory =>
    {
        string metadataPath = Path.Combine(directory, "missing-hash.json");
        var launchProbe = new LaunchProbe();
        int exitCode = RunHarness(
            [
                "run",
                "--executable", RequireCurrentExecutable(),
                "--metadata", metadataPath,
                "--execute",
            ],
            CreateFakeWindowsRuntime(launchProbe));

        Assert(exitCode == HarnessApplication.ExitUsage, "expected SHA-256 required exit code");
        Assert(launchProbe.Calls == 0, "expected SHA-256 required launch count");
        using JsonDocument document = ReadJson(metadataPath);
        AssertRefusal(document, "expected_sha256_required");
        Assert(document.RootElement.GetProperty("target").GetProperty("expected_sha256").ValueKind == JsonValueKind.Null, "missing expected SHA metadata");
    });
}

static void WrongHashRefusedWithoutLaunch()
{
    WithTemporaryDirectory(directory =>
    {
        string metadataPath = Path.Combine(directory, "hash-mismatch.json");
        string wrongDigest = new('0', 64);
        var launchProbe = new LaunchProbe();
        int exitCode = RunHarness(
            [
                "run",
                "--executable", RequireCurrentExecutable(),
                "--metadata", metadataPath,
                "--execute",
                "--expected-sha256", wrongDigest,
            ],
            CreateFakeWindowsRuntime(launchProbe));

        Assert(exitCode == HarnessApplication.ExitUsage, "hash mismatch exit code");
        Assert(launchProbe.Calls == 0, "hash mismatch launch count");
        using JsonDocument document = ReadJson(metadataPath);
        AssertRefusal(document, "executable_sha256_mismatch");
        JsonElement target = document.RootElement.GetProperty("target");
        Assert(target.GetProperty("expected_sha256").GetString() == wrongDigest, "expected digest evidence");
        Assert(target.GetProperty("observed_sha256").GetString() != wrongDigest, "observed digest evidence");
        Assert(!target.GetProperty("sha256_match").GetBoolean(), "digest mismatch evidence");
    });
}

static void CorrectHashStillRequiresWindowsRuntimeValidation()
{
    WithTemporaryDirectory(directory =>
    {
        string executable = RequireCurrentExecutable();
        string metadataPath = Path.Combine(directory, "correct-hash.json");
        var launchProbe = new LaunchProbe(exitCode: 73);
        int exitCode = RunHarness(
            [
                "run",
                "--executable", executable,
                "--metadata", metadataPath,
                "--execute",
                "--expected-sha256", ComputeSha256(executable),
            ],
            CreateFakeWindowsRuntime(launchProbe));

        Assert(exitCode == HarnessApplication.ExitUsage, "runtime validation gate exit code");
        Assert(launchProbe.Calls == 0, "runtime validation gate must block launch boundary");
        using JsonDocument document = ReadJson(metadataPath);
        JsonElement root = document.RootElement;
        AssertRefusal(document, "actual_launch_runtime_validation_required");
        Assert(root.GetProperty("target").GetProperty("sha256_match").GetBoolean(), "digest match evidence");
        Assert(!root.GetProperty("target").GetProperty("canonical_path_verified").GetBoolean(), "canonical path must remain unverified");
        Assert(!root.GetProperty("target").GetProperty("file_identity_verified").GetBoolean(), "file identity must remain unverified");
        Assert(root.GetProperty("process").GetProperty("user_sid").GetString() == "S-1-5-18", "user SID evidence");
        Assert(root.GetProperty("launch_policy").GetProperty("actual_launch_capability").GetString() == "HARD_DISABLED", "actual launch hard-disable evidence");
    });
}

static void UnsupportedPlatformRefusedWithoutLaunch()
{
    WithTemporaryDirectory(directory =>
    {
        string executable = RequireCurrentExecutable();
        string metadataPath = Path.Combine(directory, "unsupported-platform.json");
        var launchProbe = new LaunchProbe();
        var runtime = new HarnessRuntime(
            () => false,
            () => throw new InvalidOperationException("SID lookup was not expected."),
            (_, _) => throw new InvalidOperationException("Authenticode verification was not expected."),
            launchProbe.Run);
        int exitCode = RunHarness(
            [
                "run",
                "--executable", executable,
                "--metadata", metadataPath,
                "--execute",
                "--expected-sha256", ComputeSha256(executable),
            ],
            runtime);

        Assert(exitCode == HarnessApplication.ExitUsage, "unsupported platform exit code");
        Assert(launchProbe.Calls == 0, "unsupported platform launch count");
        using JsonDocument document = ReadJson(metadataPath);
        Assert(document.RootElement.GetProperty("status").GetString() == HarnessStatus.PlatformUnsupported, "unsupported platform status");
        Assert(document.RootElement.GetProperty("error").GetProperty("category").GetString() == "platform", "unsupported platform category");
    });
}

static void MetaTraderLikeTargetRequiresConfirmation()
{
    WithMetaTraderLikeTarget((directory, executable) =>
    {
        string metadataPath = Path.Combine(directory, "mt5-confirmation.json");
        var launchProbe = new LaunchProbe();
        int exitCode = RunHarness(
            [
                "run",
                "--executable", executable,
                "--metadata", metadataPath,
                "--execute",
                "--expected-sha256", ComputeSha256(executable),
            ],
            CreateFakeWindowsRuntime(launchProbe));

        Assert(exitCode == HarnessApplication.ExitUsage, "MT5 confirmation exit code");
        Assert(launchProbe.Calls == 0, "MT5 confirmation launch count");
        using JsonDocument document = ReadJson(metadataPath);
        AssertRefusal(document, "mt5_confirmation_required");
    });
}

static void MetaTraderLikeTargetRequiresSignerAllowlist()
{
    WithMetaTraderLikeTarget((directory, executable) =>
    {
        string metadataPath = Path.Combine(directory, "mt5-allowlist.json");
        var launchProbe = new LaunchProbe();
        int exitCode = RunHarness(
            [
                "run",
                "--executable", executable,
                "--metadata", metadataPath,
                "--execute",
                "--expected-sha256", ComputeSha256(executable),
                "--confirm-mt5-launch",
            ],
            CreateFakeWindowsRuntime(launchProbe));

        Assert(exitCode == HarnessApplication.ExitUsage, "MT5 allowlist exit code");
        Assert(launchProbe.Calls == 0, "MT5 allowlist launch count");
        using JsonDocument document = ReadJson(metadataPath);
        AssertRefusal(document, "authenticode_allowlist_required");
    });
}

static void MetaTraderLikeTargetInvalidAuthenticodeIsFailClosed()
{
    WithMetaTraderLikeTarget((directory, executable) =>
    {
        string metadataPath = Path.Combine(directory, "mt5-authenticode.json");
        var launchProbe = new LaunchProbe();
        var invalidSignature = new AuthenticodeVerification(
            Attempted: true,
            SignaturePresent: false,
            TrustValid: false,
            TrustProviderResult: unchecked((int)0x800B0100),
            SignerSubject: null,
            SignerThumbprintSha1: null,
            SignerThumbprintSha256: null,
            AllowlistMatched: false,
            VerificationMode: "SYNTHETIC_TEST");
        int exitCode = RunHarness(
            [
                "run",
                "--executable", executable,
                "--metadata", metadataPath,
                "--execute",
                "--expected-sha256", ComputeSha256(executable),
                "--confirm-mt5-launch",
                "--allowed-signer-subject", "CN=Expected Publisher, O=Expected Publisher",
            ],
            CreateFakeWindowsRuntime(launchProbe, invalidSignature));

        Assert(exitCode == HarnessApplication.ExitUsage, "MT5 Authenticode exit code");
        Assert(launchProbe.Calls == 0, "invalid Authenticode launch count");
        using JsonDocument document = ReadJson(metadataPath);
        AssertRefusal(document, "authenticode_signature_missing");
        JsonElement authenticode = document.RootElement.GetProperty("target").GetProperty("authenticode");
        Assert(authenticode.GetProperty("verification_attempted").GetBoolean(), "Authenticode attempted evidence");
        Assert(!authenticode.GetProperty("launch_approved").GetBoolean(), "Authenticode refusal evidence");
    });
}

static void MetaTraderLikeTargetTrustedAllowlistedRemainsHardDisabled()
{
    WithMetaTraderLikeTarget((directory, executable) =>
    {
        const string subject = "CN=Expected Publisher, O=Expected Publisher";
        const string sha256Thumbprint = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA";
        string metadataPath = Path.Combine(directory, "mt5-approved-boundary.json");
        var launchProbe = new LaunchProbe(exitCode: 41);
        var runtime = new HarnessRuntime(
            () => true,
            () => "S-1-5-18",
            (_, allowlist) => new AuthenticodeVerification(
                Attempted: true,
                SignaturePresent: true,
                TrustValid: true,
                TrustProviderResult: 0,
                SignerSubject: subject,
                SignerThumbprintSha1: null,
                SignerThumbprintSha256: sha256Thumbprint,
                AllowlistMatched: allowlist.Matches(subject, null, sha256Thumbprint),
                VerificationMode: "SYNTHETIC_TEST"),
            launchProbe.Run);

        int exitCode = RunHarness(
            [
                "run",
                "--executable", executable,
                "--metadata", metadataPath,
                "--execute",
                "--expected-sha256", ComputeSha256(executable),
                "--confirm-mt5-launch",
                "--allowed-signer-thumbprint", sha256Thumbprint,
            ],
            runtime);

        Assert(exitCode == HarnessApplication.ExitUsage, "trusted MT5 hard-disable exit code");
        Assert(launchProbe.Calls == 0, "trusted allowlisted MT5 must not reach launch boundary");
        using JsonDocument document = ReadJson(metadataPath);
        JsonElement authenticode = document.RootElement.GetProperty("target").GetProperty("authenticode");
        AssertRefusal(document, "actual_launch_runtime_validation_required");
        Assert(authenticode.GetProperty("preliminary_checks_passed").GetBoolean(), "preliminary Authenticode evidence");
        Assert(!authenticode.GetProperty("provider_signer_binding_verified").GetBoolean(), "provider signer binding must remain false");
        Assert(!authenticode.GetProperty("launch_approved").GetBoolean(), "MT5 launch must remain unapproved");
        Assert(authenticode.GetProperty("signer_subject").GetString() == subject, "sanitized signer subject evidence");
        Assert(authenticode.GetProperty("signer_thumbprint_sha256").GetString() == sha256Thumbprint, "signer thumbprint evidence");
    });
}

static void AuthenticodeAllowlistIsExact()
{
    Assert(
        AuthenticodeAllowlist.TryNormalizeThumbprint(
            "AA:AA:AA:AA:AA:AA:AA:AA:AA:AA:AA:AA:AA:AA:AA:AA:AA:AA:AA:AA",
            out string normalized),
        "thumbprint normalization");
    var allowlist = new AuthenticodeAllowlist(
        ["CN=Expected Publisher, O=Expected Publisher"],
        [normalized]);
    Assert(allowlist.Matches("cn=expected publisher, o=expected publisher", null, null), "case-insensitive exact subject");
    Assert(!allowlist.Matches("CN=Expected Publisher", null, null), "partial publisher must not match");
    Assert(allowlist.Matches(null, new string('A', 40), null), "normalized SHA-1 thumbprint");
    Assert(!allowlist.Matches(null, new string('B', 40), null), "different thumbprint must not match");
}

static void DryRunMetadataOmitsArgumentValues()
{
    WithTemporaryDirectory(directory =>
    {
        string metadataPath = Path.Combine(directory, "metadata.json");
        const string marker = "SAFE_ARGUMENT_MARKER_MUST_NOT_APPEAR";
        using var stdout = new StringWriter();
        using var stderr = new StringWriter();

        int exitCode = HarnessApplication.Execute(
            [
                "run",
                "--executable", RequireCurrentExecutable(),
                "--metadata", metadataPath,
                "--phase", "TEST",
                "--dry-run",
                "--arg", marker,
            ],
            stdout,
            stderr);

        Assert(exitCode == 0, "dry-run exit code");
        string json = File.ReadAllText(metadataPath, Encoding.UTF8);
        Assert(!json.Contains(marker, StringComparison.Ordinal), "raw argument leaked into metadata");
        using JsonDocument document = JsonDocument.Parse(json);
        JsonElement root = document.RootElement;
        Assert(root.GetProperty("status").GetString() == HarnessStatus.DryRun, "dry-run status");
        Assert(root.GetProperty("target").GetProperty("argument_count").GetInt32() == 1, "argument count");
        Assert(!root.GetProperty("evidence_hygiene").GetProperty("arguments_recorded").GetBoolean(), "argument hygiene");
        Assert(
            root.GetProperty("job_policy").GetProperty("environment_policy").GetString() == "ALLOWLIST",
            "fixed environment policy");
    });
}

static void MetadataWriterRefusesExistingFile()
{
    WithTemporaryDirectory(directory =>
    {
        string metadataPath = Path.Combine(directory, "existing.json");
        File.WriteAllText(metadataPath, "{}");
        bool refused = false;
        try
        {
            _ = new MetadataWriter(metadataPath);
        }
        catch (IOException)
        {
            refused = true;
        }

        Assert(refused, "existing metadata evidence should be refused");
    });
}

static void MetadataPathPolicyFlagsUnc()
{
    Assert(MetadataPathPolicy.IsUncOrDevicePath(@"\\server\share\evidence"), "UNC path");
    Assert(MetadataPathPolicy.IsUncOrDevicePath(@"\\?\C:\TJLab\evidence"), "device path");
    Assert(!MetadataPathPolicy.IsUncOrDevicePath(@"C:\TJLab\evidence"), "local drive path");
}

static void TargetLeaseMatchesDigest()
{
    string executable = RequireCurrentExecutable();
    using TargetExecutableLease lease = TargetExecutableLease.Open(executable);
    Assert(lease.MatchesExpectedSha256(lease.Sha256), "lease digest self-match");
    Assert(lease.MatchesExpectedSha256(lease.Sha256.ToUpperInvariant()), "lease digest case normalization");
    Assert(!lease.MatchesExpectedSha256(new string('0', 64)), "lease digest mismatch");
}

static void TargetLeaseBlocksWriteOnWindows()
{
    if (!OperatingSystem.IsWindows())
    {
        return;
    }

    WithTemporaryDirectory(directory =>
    {
        string targetPath = Path.Combine(directory, "innocent-target.bin");
        File.WriteAllText(targetPath, "stable target contents");
        using TargetExecutableLease lease = TargetExecutableLease.Open(targetPath);
        bool writeBlocked = false;
        try
        {
            using FileStream _ = new(
                targetPath,
                FileMode.Open,
                FileAccess.Write,
                FileShare.ReadWrite | FileShare.Delete);
        }
        catch (IOException)
        {
            writeBlocked = true;
        }

        Assert(writeBlocked, "target write/delete sharing should remain blocked while leased");
        Assert(lease.SizeBytes > 0, "lease metadata");
    });
}

static void OptionalWindowsActualLaunchRemainsHardDisabled()
{
    if (!LiveWindowsSmokeEnabled())
    {
        return;
    }

    string systemRoot = Environment.GetEnvironmentVariable("SystemRoot")
        ?? throw new InvalidOperationException("SystemRoot is unavailable.");
    string cmd = Path.Combine(systemRoot, "System32", "cmd.exe");
    WithTemporaryDirectory(directory =>
    {
        string metadataPath = Path.Combine(directory, "metadata.json");
        int exitCode = RunHarness(
            [
                "run",
                "--executable", cmd,
                "--metadata", metadataPath,
                "--expected-sha256", ComputeSha256(cmd),
                "--execute",
                "--phase", "SMOKE",
                "--timeout-seconds", "10",
                "--no-window",
                "--arg", "/d",
                "--arg", "/c",
                "--arg", "exit",
                "--arg", "0",
            ],
            HarnessRuntime.System);

        Assert(exitCode == HarnessApplication.ExitUsage, "Windows runtime validation gate exit code");
        using JsonDocument document = ReadJson(metadataPath);
        JsonElement root = document.RootElement;
        AssertRefusal(document, "actual_launch_runtime_validation_required");
        Assert(root.GetProperty("process").GetProperty("root_pid").ValueKind == JsonValueKind.Null, "no root PID");
    });
}

static void OptionalWindowsDescendantSmokeRemainsHardDisabled()
{
    if (!LiveWindowsSmokeEnabled())
    {
        return;
    }

    string executable = RequireCurrentExecutable();
    WithTemporaryDirectory(directory =>
    {
        string metadataPath = Path.Combine(directory, "metadata.json");
        string childReadyPath = Path.Combine(directory, "child-ready.txt");
        var childArguments = new List<string>();
        if (Path.GetFileNameWithoutExtension(executable).Equals("dotnet", StringComparison.OrdinalIgnoreCase))
        {
            childArguments.Add(Assembly.GetExecutingAssembly().Location);
        }

        childArguments.Add("--innocent-process-tree");
        childArguments.Add(childReadyPath);

        var harnessArguments = new List<string>
        {
            "run",
            "--executable", executable,
            "--metadata", metadataPath,
            "--expected-sha256", ComputeSha256(executable),
            "--execute",
            "--phase", "DESCENDANT_SMOKE",
            "--timeout-seconds", "3",
            "--no-window",
        };
        foreach (string childArgument in childArguments)
        {
            harnessArguments.Add("--arg");
            harnessArguments.Add(childArgument);
        }

        int exitCode = RunHarness(harnessArguments.ToArray(), HarnessRuntime.System);
        Assert(exitCode == HarnessApplication.ExitUsage, "descendant smoke hard-disable exit code");
        Assert(!File.Exists(childReadyPath), "descendant must not start while actual launch is disabled");

        using JsonDocument document = ReadJson(metadataPath);
        AssertRefusal(document, "actual_launch_runtime_validation_required");
    });
}

static void OptionalWindowsJobObjectNaturalExitRuntimeSmoke()
{
    if (!LiveWindowsRuntimeSmokeEnabled())
    {
        return;
    }

    string systemRoot = Environment.GetEnvironmentVariable("SystemRoot")
        ?? throw new InvalidOperationException("SystemRoot is unavailable.");
    string cmd = Path.Combine(systemRoot, "System32", "cmd.exe");
    WithTemporaryDirectory(directory =>
    {
        using var lease = TargetExecutableLease.Open(cmd);
        string metadataPath = Path.Combine(directory, "metadata.json");

        var options = new LaunchOptions(
            cmd,
            ["/d", "/c", "exit", "0"],
            Path.GetDirectoryName(cmd)!,
            metadataPath,
            ComputeSha256(cmd),
            Guid.NewGuid().ToString("D"),
            "RUNTIME_SMOKE",
            null,
            ExecuteRequested: false,
            DryRun: false,
            NoWindow: true,
            ConfirmMetaTraderLaunch: false,
            AllowedSignerSubjects: [],
            AllowedSignerThumbprints: []);

        HarnessMetadata metadata = HarnessMetadataFactory.Create(options, lease, "S-1-5-18");
        metadata.Status = HarnessStatus.Validated;
        using var writer = new MetadataWriter(metadataPath);

        JobRunResult result = JobObjectRunner.Run(options, metadata, writer);
        Assert(result.ExitCode == 0, "natural exit runtime smoke exit code");
        Assert(metadata.Status == HarnessStatus.Completed, "natural exit runtime status");
        Assert(metadata.Process.RootPid is not null, "natural exit root pid");
        Assert(metadata.Process.PrimaryThreadId is not null, "natural exit primary thread id");
        Assert(metadata.Process.WindowsSessionId is not null, "natural exit session id");
        Assert(metadata.Process.KernelCreationUtc is not null, "natural exit kernel creation utc");
        Assert(metadata.Process.CreatedSuspended, "natural exit created suspended");
        Assert(metadata.Process.AssignedBeforeResume, "natural exit assigned before resume");
        Assert(metadata.Process.ResumePreviousSuspendCount == 1, "natural exit resume suspend count");
        Assert(metadata.JobPolicy.KillOnJobCloseVerified == true, "natural exit kill_on_job_close_verified");
        Assert(metadata.JobPolicy.BreakawayAllowed == false, "natural exit breakaway flag false");
        Assert(metadata.JobPolicy.SilentBreakawayAllowed == false, "natural exit silent breakaway flag false");
        Assert(metadata.ProcessResult.JobTotalProcesses >= 1, "natural exit job process count");
        Assert(!metadata.ProcessResult.TimedOut, "natural exit timed out false");
        Assert(!metadata.ProcessResult.Cancelled, "natural exit cancelled false");
        Assert(metadata.ProcessResult.PrimaryExitCode == 0u, "natural exit primary exit code");
        Assert(metadata.LaunchPolicy.ActualLaunchCapability == "HARD_DISABLED", "natural exit hard-disabled launch policy");

        string json = File.ReadAllText(metadataPath, Encoding.UTF8);
        Assert(!json.Contains("/d", StringComparison.Ordinal), "natural exit metadata omitted /d");
        Assert(!json.Contains("/c", StringComparison.Ordinal), "natural exit metadata omitted /c");
        Assert(!json.Contains("exit 0", StringComparison.Ordinal), "natural exit metadata omitted command line");
        Assert(!json.Contains("password", StringComparison.OrdinalIgnoreCase), "natural exit metadata no password");
        Assert(!json.Contains("login", StringComparison.OrdinalIgnoreCase), "natural exit metadata no login");
        Assert(!json.Contains("token", StringComparison.OrdinalIgnoreCase), "natural exit metadata no token");
    });
}

static void OptionalWindowsJobObjectDescendantTimeoutRuntimeSmoke()
{
    if (!LiveWindowsRuntimeSmokeEnabled())
    {
        return;
    }

    string executable = RequireCurrentExecutable();
    WithTemporaryDirectory(directory =>
    {
        string metadataPath = Path.Combine(directory, "metadata.json");
        string childReadyPath = Path.Combine(directory, "child-ready.txt");
        var arguments = new List<string> { "--innocent-process-tree", childReadyPath };
        using var lease = TargetExecutableLease.Open(executable);
        var options = new LaunchOptions(
            executable,
            arguments,
            Path.GetDirectoryName(executable)!,
            metadataPath,
            ComputeSha256(executable),
            Guid.NewGuid().ToString("D"),
            "RUNTIME_SMOKE_TIMEOUT",
            TimeSpan.FromSeconds(6),
            false,
            false,
            true,
            false,
            [],
            []);

        HarnessMetadata metadata = HarnessMetadataFactory.Create(options, lease, "S-1-5-18");
        metadata.Status = HarnessStatus.Validated;
        using var writer = new MetadataWriter(metadataPath);

        JobRunResult result = JobObjectRunner.Run(options, metadata, writer);
        Assert(result.ExitCode == 124, "descendant timeout runtime smoke exit code");
        Assert(metadata.Status == HarnessStatus.TimedOut, "descendant timeout status");
        Assert(metadata.ProcessResult.TimedOut, "descendant timeout metadata timed out");
        Assert(!metadata.ProcessResult.Cancelled, "descendant timeout cancelled false");
        Assert(metadata.ProcessResult.JobTotalProcesses >= 2, "descendant timeout job process count");
        Assert(metadata.Process.CreatedSuspended, "descendant created suspended");
        Assert(metadata.Process.AssignedBeforeResume, "descendant assigned before resume");
        Assert(metadata.Process.ResumePreviousSuspendCount == 1, "descendant resume suspend count");
        Assert(metadata.JobPolicy.KillOnJobCloseVerified == true, "descendant kill_on_job_close_verified");
        Assert(metadata.JobPolicy.BreakawayAllowed == false, "descendant breakaway flag false");
        Assert(metadata.JobPolicy.SilentBreakawayAllowed == false, "descendant silent breakaway flag false");

        (int childPid, long childStartTime) = ParseChildReadyRecord(childReadyPath);
        Assert(
            WaitUntilProcessHasTerminatedOrChanged(
                childPid,
                childStartTime,
                TimeSpan.FromSeconds(5)),
            "descendant child process did not terminate");
        Assert(
            WaitUntilProcessNotFound(
                metadata.Process.RootPid!.Value,
                TimeSpan.FromSeconds(5)),
            "descendant root process did not terminate");
    });
}

static HarnessRuntime CreateFakeWindowsRuntime(
    LaunchProbe launchProbe,
    AuthenticodeVerification? authenticodeVerification = null) =>
    new(
        () => true,
        () => "S-1-5-18",
        (_, _) => authenticodeVerification
            ?? throw new InvalidOperationException("Authenticode verification was not expected."),
        launchProbe.Run);

static int RunHarness(string[] arguments, HarnessRuntime runtime)
{
    using var stdout = new StringWriter();
    using var stderr = new StringWriter();
    return HarnessApplication.Execute(arguments, stdout, stderr, runtime);
}

static void AssertRefusal(JsonDocument document, string category)
{
    JsonElement root = document.RootElement;
    Assert(root.GetProperty("status").GetString() == HarnessStatus.Refused, "refusal status");
    Assert(root.GetProperty("error").GetProperty("category").GetString() == category, "refusal category");
    Assert(!root.GetProperty("evidence_hygiene").GetProperty("arguments_recorded").GetBoolean(), "refusal argument hygiene");
    Assert(!root.GetProperty("evidence_hygiene").GetProperty("environment_values_recorded").GetBoolean(), "refusal environment hygiene");
}

static JsonDocument ReadJson(string path) =>
    JsonDocument.Parse(File.ReadAllText(path, Encoding.UTF8));

static string ComputeSha256(string path)
{
    using FileStream stream = File.OpenRead(path);
    return Convert.ToHexString(SHA256.HashData(stream)).ToLowerInvariant();
}

static void WithMetaTraderLikeTarget(Action<string, string> body)
{
    WithTemporaryDirectory(directory =>
    {
        string target = Path.Combine(directory, "terminal64.exe");
        File.Copy(RequireCurrentExecutable(), target);
        body(directory, target);
    });
}

static void WithTemporaryDirectory(Action<string> body)
{
    string directory = Path.Combine(Path.GetTempPath(), $"jobharness-test-{Guid.NewGuid():N}");
    Directory.CreateDirectory(directory);
    try
    {
        body(directory);
    }
    finally
    {
        Directory.Delete(directory, recursive: true);
    }
}

static bool LiveWindowsSmokeEnabled() =>
    OperatingSystem.IsWindows()
    && Environment.GetEnvironmentVariable("JOBHARNESS_RUN_LIVE_SMOKE") == "1";

static bool LiveWindowsRuntimeSmokeEnabled() =>
    OperatingSystem.IsWindows()
    && Environment.GetEnvironmentVariable("JOBHARNESS_RUN_RUNTIME_SMOKE") == "1";

static (int ChildPid, long ChildStartTime) ParseChildReadyRecord(string path)
{
    string raw = File.ReadAllText(path, Encoding.UTF8).Trim();
    Assert(!string.IsNullOrWhiteSpace(raw), "child ready record exists");
    string[] parts = raw.Split('|', 2, StringSplitOptions.RemoveEmptyEntries);
    Assert(parts.Length == 2, "child ready record has pid and start time");

    bool parsedPid = int.TryParse(parts[0], out int pid);
    bool parsedStartTime = long.TryParse(parts[1], out long startTime);
    Assert(parsedPid, "child pid parsed from ready record");
    Assert(parsedStartTime, "child start time parsed from ready record");

    return (pid, startTime);
}

static bool WaitUntilProcessNotFound(int? processId, TimeSpan timeout)
{
    if (processId is null)
    {
        return true;
    }

    Stopwatch timer = Stopwatch.StartNew();
    while (timer.Elapsed < timeout)
    {
        if (Process.GetProcesses().All(process => process.Id != processId.Value))
        {
            return true;
        }

        Thread.Sleep(50);
    }

    return false;
}

static bool WaitUntilProcessHasTerminatedOrChanged(int processId, long expectedStartTime, TimeSpan timeout)
{
    Stopwatch timer = Stopwatch.StartNew();
    while (timer.Elapsed < timeout)
    {
        Process? process = Process.GetProcesses().FirstOrDefault(process => process.Id == processId);
        if (process is null)
        {
            return true;
        }

        using (process)
        {
            long observedStartTime;
            try
            {
                observedStartTime = process.StartTime.ToUniversalTime().Ticks;
            }
            catch (Exception exception) when (exception is Win32Exception or InvalidOperationException)
            {
                return true;
            }

            if (observedStartTime != expectedStartTime)
            {
                return true;
            }
        }

        Thread.Sleep(50);
    }

    return false;
}

static ProcessStartInfo CreateSelfStartInfo(params string[] childArguments)
{
    string executable = RequireCurrentExecutable();
    var startInfo = new ProcessStartInfo
    {
        FileName = executable,
        UseShellExecute = false,
        CreateNoWindow = true,
    };

    if (Path.GetFileNameWithoutExtension(executable).Equals("dotnet", StringComparison.OrdinalIgnoreCase))
    {
        startInfo.ArgumentList.Add(Assembly.GetExecutingAssembly().Location);
    }

    foreach (string argument in childArguments)
    {
        startInfo.ArgumentList.Add(argument);
    }

    return startInfo;
}

static string RequireCurrentExecutable() =>
    Environment.ProcessPath ?? throw new InvalidOperationException("Current process path is unavailable.");

static void Assert(bool condition, string message)
{
    if (!condition)
    {
        throw new InvalidOperationException(message);
    }
}

internal sealed class LaunchProbe
{
    private readonly int _exitCode;

    public LaunchProbe(int exitCode = 0)
    {
        _exitCode = exitCode;
    }

    public int Calls { get; private set; }

    public JobRunResult Run(LaunchOptions options, HarnessMetadata metadata, MetadataWriter writer)
    {
        ArgumentNullException.ThrowIfNull(options);
        ArgumentNullException.ThrowIfNull(metadata);
        ArgumentNullException.ThrowIfNull(writer);
        Calls++;
        return new JobRunResult(_exitCode);
    }
}

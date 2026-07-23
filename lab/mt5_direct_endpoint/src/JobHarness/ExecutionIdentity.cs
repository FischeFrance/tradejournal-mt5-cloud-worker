using System.Runtime.Versioning;
using System.Security;
using System.Security.Principal;

namespace TradeJournal.Lab.JobHarness;

internal static class ExecutionIdentity
{
    public static string? GetCurrentUserSid()
    {
        if (!OperatingSystem.IsWindows())
        {
            return null;
        }

        return GetCurrentWindowsUserSid();
    }

    [SupportedOSPlatform("windows")]
    private static string? GetCurrentWindowsUserSid()
    {
        try
        {
            using WindowsIdentity identity = WindowsIdentity.GetCurrent();
            return identity.User?.Value;
        }
        catch (Exception exception) when (
            exception is SecurityException or UnauthorizedAccessException or InvalidOperationException)
        {
            return null;
        }
    }
}

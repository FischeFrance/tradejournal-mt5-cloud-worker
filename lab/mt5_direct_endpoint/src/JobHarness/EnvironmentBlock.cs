using System.Runtime.InteropServices;

namespace TradeJournal.Lab.JobHarness;

internal sealed class EnvironmentBlock : IDisposable
{
    private static readonly string[] AllowedNames =
    [
        "APPDATA",
        "COMPUTERNAME",
        "ComSpec",
        "CommonProgramFiles",
        "CommonProgramFiles(x86)",
        "HOMEDRIVE",
        "HOMEPATH",
        "LOCALAPPDATA",
        "NUMBER_OF_PROCESSORS",
        "OS",
        "PATH",
        "PATHEXT",
        "PROCESSOR_ARCHITECTURE",
        "ProgramData",
        "ProgramFiles",
        "ProgramFiles(x86)",
        "SystemDrive",
        "SystemRoot",
        "TEMP",
        "TMP",
        "USERDOMAIN",
        "USERNAME",
        "USERPROFILE",
        "WINDIR",
    ];

    private IntPtr _pointer;

    private EnvironmentBlock(IntPtr pointer)
    {
        _pointer = pointer;
    }

    public IntPtr Pointer => _pointer;

    public static EnvironmentBlock CreateAllowlisted()
    {
        var entries = new SortedDictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        foreach (string name in AllowedNames)
        {
            string? value = Environment.GetEnvironmentVariable(name);
            if (value is not null && !value.Contains('\0'))
            {
                entries[name] = value;
            }
        }

        // CreateProcessW expects a sorted sequence of null-terminated NAME=VALUE strings,
        // followed by one additional null terminator.
        string block = string.Join('\0', entries.Select(pair => $"{pair.Key}={pair.Value}")) + "\0\0";
        return new EnvironmentBlock(Marshal.StringToHGlobalUni(block));
    }

    public void Dispose()
    {
        IntPtr pointer = Interlocked.Exchange(ref _pointer, IntPtr.Zero);
        if (pointer != IntPtr.Zero)
        {
            Marshal.FreeHGlobal(pointer);
        }

        GC.SuppressFinalize(this);
    }
}

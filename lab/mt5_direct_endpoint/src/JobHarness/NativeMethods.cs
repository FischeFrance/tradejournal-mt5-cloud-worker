using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;
using System.Text;
using Microsoft.Win32.SafeHandles;

namespace TradeJournal.Lab.JobHarness;

[Flags]
internal enum JobObjectLimitFlags : uint
{
    KillOnJobClose = 0x00002000,
    BreakawayOk = 0x00000800,
    SilentBreakawayOk = 0x00001000,
}

[Flags]
internal enum ProcessCreationFlags : uint
{
    CreateSuspended = 0x00000004,
    CreateUnicodeEnvironment = 0x00000400,
    CreateNoWindow = 0x08000000,
}

internal enum JobObjectInformationClass
{
    BasicAccountingInformation = 1,
    ExtendedLimitInformation = 9,
}

[StructLayout(LayoutKind.Sequential)]
internal struct JobObjectBasicLimitInformation
{
    public long PerProcessUserTimeLimit;
    public long PerJobUserTimeLimit;
    public JobObjectLimitFlags LimitFlags;
    public UIntPtr MinimumWorkingSetSize;
    public UIntPtr MaximumWorkingSetSize;
    public uint ActiveProcessLimit;
    public UIntPtr Affinity;
    public uint PriorityClass;
    public uint SchedulingClass;
}

[StructLayout(LayoutKind.Sequential)]
internal struct IoCounters
{
    public ulong ReadOperationCount;
    public ulong WriteOperationCount;
    public ulong OtherOperationCount;
    public ulong ReadTransferCount;
    public ulong WriteTransferCount;
    public ulong OtherTransferCount;
}

[StructLayout(LayoutKind.Sequential)]
internal struct JobObjectExtendedLimitInformation
{
    public JobObjectBasicLimitInformation BasicLimitInformation;
    public IoCounters IoInfo;
    public UIntPtr ProcessMemoryLimit;
    public UIntPtr JobMemoryLimit;
    public UIntPtr PeakProcessMemoryUsed;
    public UIntPtr PeakJobMemoryUsed;
}

[StructLayout(LayoutKind.Sequential)]
internal struct JobObjectBasicAccountingInformation
{
    public long TotalUserTime;
    public long TotalKernelTime;
    public long ThisPeriodTotalUserTime;
    public long ThisPeriodTotalKernelTime;
    public uint TotalPageFaultCount;
    public uint TotalProcesses;
    public uint ActiveProcesses;
    public uint TotalTerminatedProcesses;
}

[StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
internal struct StartupInfo
{
    public uint Cb;
    public string? Reserved;
    public string? Desktop;
    public string? Title;
    public uint X;
    public uint Y;
    public uint XSize;
    public uint YSize;
    public uint XCountChars;
    public uint YCountChars;
    public uint FillAttribute;
    public uint Flags;
    public ushort ShowWindow;
    public ushort Reserved2Length;
    public IntPtr Reserved2;
    public IntPtr StandardInput;
    public IntPtr StandardOutput;
    public IntPtr StandardError;
}

[StructLayout(LayoutKind.Sequential)]
internal struct ProcessInformation
{
    public IntPtr Process;
    public IntPtr Thread;
    public uint ProcessId;
    public uint ThreadId;
}

internal sealed class SafeJobHandle : SafeHandleZeroOrMinusOneIsInvalid
{
    private SafeJobHandle()
        : base(ownsHandle: true)
    {
    }

    protected override bool ReleaseHandle() => NativeMethods.CloseHandle(handle);
}

internal sealed class SafeKernelObjectHandle : SafeHandleZeroOrMinusOneIsInvalid
{
    public SafeKernelObjectHandle(IntPtr preexistingHandle, bool ownsHandle)
        : base(ownsHandle)
    {
        SetHandle(preexistingHandle);
    }

    protected override bool ReleaseHandle() => NativeMethods.CloseHandle(handle);
}

internal static class NativeMethods
{
    internal const uint WaitObject0 = 0x00000000;
    internal const uint WaitTimeout = 0x00000102;
    internal const uint WaitFailed = 0xFFFFFFFF;
    internal const uint StillActive = 259;

    [DllImport("kernel32.dll", EntryPoint = "CreateJobObjectW", CharSet = CharSet.Unicode, ExactSpelling = true, SetLastError = true)]
    internal static extern SafeJobHandle CreateJobObject(IntPtr jobAttributes, string? name);

    [DllImport("kernel32.dll", ExactSpelling = true, SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    internal static extern bool SetInformationJobObject(
        SafeJobHandle job,
        JobObjectInformationClass informationClass,
        ref JobObjectExtendedLimitInformation information,
        uint informationLength);

    [DllImport("kernel32.dll", ExactSpelling = true, SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    internal static extern bool QueryInformationJobObject(
        SafeJobHandle job,
        JobObjectInformationClass informationClass,
        out JobObjectExtendedLimitInformation information,
        uint informationLength,
        out uint returnLength);

    [DllImport("kernel32.dll", ExactSpelling = true, SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    internal static extern bool QueryInformationJobObject(
        SafeJobHandle job,
        JobObjectInformationClass informationClass,
        out JobObjectBasicAccountingInformation information,
        uint informationLength,
        out uint returnLength);

    [DllImport("kernel32.dll", ExactSpelling = true, SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    internal static extern bool AssignProcessToJobObject(SafeJobHandle job, SafeKernelObjectHandle process);

    [DllImport("kernel32.dll", EntryPoint = "CreateProcessW", CharSet = CharSet.Unicode, ExactSpelling = true, SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    internal static extern bool CreateProcess(
        string applicationName,
        StringBuilder commandLine,
        IntPtr processAttributes,
        IntPtr threadAttributes,
        [MarshalAs(UnmanagedType.Bool)] bool inheritHandles,
        ProcessCreationFlags creationFlags,
        IntPtr environment,
        string currentDirectory,
        ref StartupInfo startupInfo,
        out ProcessInformation processInformation);

    [DllImport("kernel32.dll", ExactSpelling = true, SetLastError = true)]
    internal static extern uint ResumeThread(SafeKernelObjectHandle thread);

    [DllImport("kernel32.dll", ExactSpelling = true, SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    internal static extern bool TerminateJobObject(SafeJobHandle job, uint exitCode);

    [DllImport("kernel32.dll", ExactSpelling = true, SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    internal static extern bool TerminateProcess(SafeKernelObjectHandle process, uint exitCode);

    [DllImport("kernel32.dll", ExactSpelling = true, SetLastError = true)]
    internal static extern uint WaitForSingleObject(SafeKernelObjectHandle handle, uint milliseconds);

    [DllImport("kernel32.dll", ExactSpelling = true, SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    internal static extern bool GetExitCodeProcess(SafeKernelObjectHandle process, out uint exitCode);

    [DllImport("kernel32.dll", ExactSpelling = true, SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    internal static extern bool GetProcessTimes(
        SafeKernelObjectHandle process,
        out FILETIME creationTime,
        out FILETIME exitTime,
        out FILETIME kernelTime,
        out FILETIME userTime);

    [DllImport("kernel32.dll", ExactSpelling = true, SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    internal static extern bool ProcessIdToSessionId(uint processId, out uint sessionId);

    [DllImport("kernel32.dll", ExactSpelling = true, SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    internal static extern bool CloseHandle(IntPtr handle);
}

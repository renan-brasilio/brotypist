# CI-only launcher shared by canonical xtask and authenticated artifact reuse.
# Never distribute this script or use it against a normal Windows profile.
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$InputFile,
    [Parameter(Mandatory)][string]$Work,
    [string]$ReportDirectory,
    [switch]$Bootstrap,
    [string]$HandoffHash
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$metadata = [ordered]@{
    schema = 1; tool = 'windows-standard-user-installer-client'; passed = $false
    classification = 'failed'; teardown_required = $false; uninstall_verified = $false
    actual_standard_user_token = $null
    genuine_standard_user = $false; policy_scope = 'HKLM64 DisableMSI absent-to-DWORD-zero only; disposable GitHub runner'
    policy_modified = $false; restoration_verified = $false
    before = $null; during = $null; restored = $null; policy_key_created = $false
    transactional_policy_contract = 'KTM RegOpen/Create/DeleteKeyTransacted; conflict/unsupported fails closed; no ordinary write/delete fallback'
    quiescence_verified = $false; account_cleanup_verified = $false; profile_cleanup_verified = $false
    private_tree_cleanup_verified = $false; global_acl_changes = $false; alternate_credentials = $true
    stage = 'preflight'; diagnostics = [Collections.Generic.List[string]]::new()
    failure = $null; cleanup_failure = $null
}
$ownedRoot = $null; $account = $null; $accountName = $null; $validationSid = $null
$password = $null; $coreWork = $null; $profileWasAbsent = $false; $outcome = $null
$accountCreationAttempted = $false
$ownedRootCreated = $false
$reportDirectoryValidated = $false
$ownedProfilePath = $null
$base = $null; $key = $null; $createdKey = $false; $createdValue = $false
$nativeInitialized = $false
$nativeLifecycleSucceeded = $false
$policyPath = 'Software\Policies\Microsoft\Windows\Installer'
function Require([bool]$Condition, [string]$Message) { if (-not $Condition) { throw $Message } }
function Assert-DisableMsiPolicy($Policy) {
    Require (-not $Policy.present -or ($Policy.kind -ceq 'DWord' -and $Policy.value -eq 0)) 'Explicit restrictive/type-invalid DisableMSI refused; never overridden'
}
function Assert-NativeQuiescence($Outcome) {
    Require ($Outcome.quiescence.installer_transactions_quiescent -is [bool] -and $Outcome.quiescence.installer_transactions_quiescent -and $Outcome.quiescence.owned_processes_quiescent -is [bool] -and $Outcome.quiescence.owned_processes_quiescent) 'Native MSI transactions and owned processes must independently attest quiescence'
}
function Policy-Snapshot($Key) {
    $result = [ordered]@{ key_present = $null -ne $Key; values = [ordered]@{} }
    foreach ($name in @('DisableMSI', 'DisableUserInstalls', 'AlwaysInstallElevated')) {
        $present = $null -ne $Key -and $Key.GetValueNames() -icontains $name
        $kind = if ($present) { $Key.GetValueKind($name).ToString() } else { $null }
        $valueHash = $null
        if ($present) {
            $encoded = [Text.UTF8Encoding]::new($false).GetBytes((ConvertTo-Json -InputObject $Key.GetValue($name, $null, [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames) -Compress))
            $valueHash = [Convert]::ToHexString([Security.Cryptography.SHA256]::HashData($encoded)).ToLowerInvariant()
        }
        $result.values[$name] = [ordered]@{ present = $present; kind = $kind; value = if ($kind -ceq 'DWord') { $Key.GetValue($name) } else { $null }; value_sha256 = $valueHash; non_dword_value_retained = $false }
    }
    return $result
}
function Initialize-InstallerNative {
    # LOGON_WITH_PROFILE + NULL environment: a real local logon with its own
    # profile environment, never SAFER, impersonation, or inherited CI secrets.
    Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Text;
public static class BroTypistStandardUserInstallerClient {
    public static bool LogonAttempted, ClientLaunched, ClientResumed, ClientExited, DeadlineExceeded, JobQuiescent, OwnershipUnresolved;
    public static string FailureStage, CleanupFailureStage;
    public static int FailureNativeError, CleanupNativeError;
    public static string Stage="native_setup";
    public static int LastNativeError;
    static IntPtr station,desktop,job;
    [StructLayout(LayoutKind.Sequential, CharSet=CharSet.Unicode)]
    struct Startup {
        public int size;
        public string reserved,desktop,title;
        public uint x,y,width,height,xChars,yChars,fill,flags;
        public ushort show,reservedCount;
        public IntPtr reservedBytes,input,output,error;
    }
    [StructLayout(LayoutKind.Sequential)] struct ProcessInfo {
        public IntPtr process,thread;
        public uint processId,threadId;
    }
    [StructLayout(LayoutKind.Sequential)] struct SecurityAttributes {
        public int size; public IntPtr descriptor; public int inherit;
    }
    [StructLayout(LayoutKind.Sequential)] struct JobAccounting {
        public long user,kernel,periodUser,periodKernel;
        public uint faults,total,active,terminated;
    }
    [DllImport("advapi32.dll", CharSet=CharSet.Unicode, ExactSpelling=true, SetLastError=true)]
    static extern bool CreateProcessWithLogonW(string user,string domain,IntPtr password,uint logonFlags,string application,StringBuilder command,uint flags,IntPtr environment,string directory,ref Startup startup,out ProcessInfo process);
    [DllImport("advapi32.dll", CharSet=CharSet.Unicode, ExactSpelling=true, SetLastError=true)]
    static extern bool ConvertStringSecurityDescriptorToSecurityDescriptorW(string sddl,uint revision,out IntPtr descriptor,IntPtr size);
    [DllImport("user32.dll", CharSet=CharSet.Unicode, ExactSpelling=true, SetLastError=true)]
    static extern IntPtr CreateWindowStationW(string name,uint flags,uint access,ref SecurityAttributes attributes);
    [DllImport("user32.dll", CharSet=CharSet.Unicode, ExactSpelling=true, SetLastError=true)]
    static extern IntPtr CreateDesktopW(string name,IntPtr device,IntPtr mode,uint flags,uint access,ref SecurityAttributes attributes);
    [DllImport("user32.dll", SetLastError=true)] static extern IntPtr GetProcessWindowStation();
    [DllImport("user32.dll", SetLastError=true)] static extern bool SetProcessWindowStation(IntPtr station);
    [DllImport("kernel32.dll")] static extern uint GetCurrentThreadId();
    [DllImport("user32.dll", SetLastError=true)] static extern IntPtr GetThreadDesktop(uint thread);
    [DllImport("user32.dll", SetLastError=true)] static extern bool SetThreadDesktop(IntPtr desktop);
    [DllImport("user32.dll", SetLastError=true)] static extern bool CloseWindowStation(IntPtr station);
    [DllImport("user32.dll", SetLastError=true)] static extern bool CloseDesktop(IntPtr desktop);
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode, ExactSpelling=true, SetLastError=true)] static extern IntPtr CreateJobObjectW(IntPtr attributes,string name);
    [DllImport("kernel32.dll", SetLastError=true)] static extern bool AssignProcessToJobObject(IntPtr job,IntPtr process);
    [DllImport("kernel32.dll", SetLastError=true)] static extern bool QueryInformationJobObject(IntPtr job,int kind,out JobAccounting info,int size,IntPtr length);
    [DllImport("kernel32.dll", SetLastError=true)] static extern uint ResumeThread(IntPtr thread);
    [DllImport("kernel32.dll", SetLastError=true)] static extern bool TerminateProcess(IntPtr process,uint exit);
    [DllImport("kernel32.dll", SetLastError=true)] static extern uint WaitForSingleObject(IntPtr handle,uint timeout);
    [DllImport("kernel32.dll", SetLastError=true)] static extern bool GetExitCodeProcess(IntPtr process,out uint exit);
    [DllImport("kernel32.dll", SetLastError=true)] static extern bool CloseHandle(IntPtr handle);
    [DllImport("kernel32.dll")] static extern IntPtr LocalFree(IntPtr memory);
    [DllImport("userenv.dll", CharSet=CharSet.Unicode, ExactSpelling=true, SetLastError=true)]
    static extern bool DeleteProfileW(string sid,string path,string computer);
    static void Check(bool value) {
        if(!value) { LastNativeError=Marshal.GetLastWin32Error(); throw new Win32Exception(LastNativeError); }
    }
    static void FailOwnership() {
        OwnershipUnresolved=true;
        var failure=new InvalidOperationException("Native ownership unresolved; disposable VM teardown required");
        failure.Data["stage"]=Stage;
        failure.Data["native_error"]=LastNativeError;
        failure.Data["deadline_exceeded"]=DeadlineExceeded;
        throw failure;
    }
    static void WaitOwned(IntPtr process,uint timeout) {
        uint wait=WaitForSingleObject(process,timeout);
        if(wait==258) DeadlineExceeded=true;
        if(wait!=0) {
            if(wait==0xffffffff) LastNativeError=Marshal.GetLastWin32Error();
            FailOwnership();
        }
        ClientExited=true;
    }
    static void WaitJobQuiescent() {
        Stage="job_quiescence";
        var deadline=System.Diagnostics.Stopwatch.StartNew();
        for(;;) {
            JobAccounting accounting;
            Check(QueryInformationJobObject(job,1,out accounting,Marshal.SizeOf<JobAccounting>(),IntPtr.Zero));
            if(accounting.active==0) { JobQuiescent=true; return; }
            if(deadline.ElapsedMilliseconds>=30000) { DeadlineExceeded=true; FailOwnership(); }
            System.Threading.Thread.Sleep(100);
        }
    }
    public static int Run(string executable,string command,string directory,string user,System.Security.SecureString password,string expectedSid,string supervisorSid) {
        IntPtr descriptor=IntPtr.Zero,secret=IntPtr.Zero;
        Exception primaryFailure=null;
        ProcessInfo process=new ProcessInfo();
        if(command.Length>=1024) throw new InvalidOperationException("Logon command exceeds native limit");
        string stationName="btci-"+Guid.NewGuid().ToString("N");
        try {
            Stage="private_desktop";
            // New private objects only; no shared ACL or visible input-desktop switch.
            string sddl="D:P(A;;GA;;;SY)(A;;GA;;;"+supervisorSid+")(A;;GA;;;"+expectedSid+")";
            Check(ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl,1,out descriptor,IntPtr.Zero));
            var attributes=new SecurityAttributes { size=Marshal.SizeOf<SecurityAttributes>(),descriptor=descriptor };
            IntPtr original=GetProcessWindowStation(); Check(original!=IntPtr.Zero);
            IntPtr originalDesktop=GetThreadDesktop(GetCurrentThreadId()); Check(originalDesktop!=IntPtr.Zero);
            // GENERIC_ALL maps to the noninteractive station's supported rights.
            station=CreateWindowStationW(stationName,1,0x10000000,ref attributes); Check(station!=IntPtr.Zero);
            Exception desktopFailure=null;
            try {
                Check(SetProcessWindowStation(station));
                desktop=CreateDesktopW("Probe",IntPtr.Zero,IntPtr.Zero,0,0x10000000,ref attributes);
                Check(desktop!=IntPtr.Zero);
            } catch(Exception failure) { desktopFailure=failure; throw; }
            finally {
                // CreateDesktop assigns the desktop to this thread. Restore the
                // process station first, then its thread binding; borrowed handles
                // remain owned by Windows and are never closed here.
                bool restored=SetProcessWindowStation(original);
                if(restored) restored=SetThreadDesktop(originalDesktop);
                if(!restored) {
                    LastNativeError=Marshal.GetLastWin32Error();
                    OwnershipUnresolved=true;
                    CleanupFailureStage="desktop_context_restoration"; CleanupNativeError=LastNativeError;
                    if(desktopFailure==null) FailOwnership();
                }
            }
            job=CreateJobObjectW(IntPtr.Zero,null); Check(job!=IntPtr.Zero);
            Stage="profile_logon";
            var startup=new Startup { size=Marshal.SizeOf<Startup>(),desktop=stationName+@"\Probe" };
            secret=Marshal.SecureStringToGlobalAllocUnicode(password);
            try {
                // LOGON_WITH_PROFILE=1; CREATE_SUSPENDED=4. NULL environment
                // means target-profile environment, NOT the runner environment.
                LogonAttempted=true;
                Check(CreateProcessWithLogonW(user,".",secret,1,executable,new StringBuilder(command),4,IntPtr.Zero,directory,ref startup,out process));
                ClientLaunched=true;
            } finally { Marshal.ZeroFreeGlobalAllocUnicode(secret); secret=IntPtr.Zero; password.Dispose(); }
            Stage="job_assignment";
            Check(AssignProcessToJobObject(job,process.process));
            Stage="core_lifecycle";
            uint previousSuspendCount=ResumeThread(process.thread);
            ClientResumed=previousSuspendCount!=0xffffffff;
            Check(previousSuspendCount!=0xffffffff);
            if(previousSuspendCount!=1) throw new InvalidOperationException("Unexpected child suspend count");
            WaitOwned(process.process,900000);
            WaitJobQuiescent();
            uint code; Check(GetExitCodeProcess(process.process,out code));
            Stage="child_exited";
            return unchecked((int)code);
        } catch(Exception failure) {
            primaryFailure=failure; FailureStage=Stage;
            var nativeFailure=failure as Win32Exception;
            FailureNativeError=nativeFailure!=null ? nativeFailure.NativeErrorCode : LastNativeError;
            throw;
        }
        finally {
            try {
                if(ClientLaunched && !ClientExited) {
                    if(ClientResumed) FailOwnership();
                    // Only the never-resumed child may be terminated safely.
                    Stage="suspended_child_cleanup";
                    Check(TerminateProcess(process.process,1));
                    WaitOwned(process.process,30000);
                    WaitJobQuiescent();
                }
            } catch {
                OwnershipUnresolved=true;
                CleanupFailureStage=Stage; CleanupNativeError=LastNativeError;
                // Preserve the first failure; cleanup is separate evidence.
                if(primaryFailure==null) throw;
            } finally {
                if(process.thread!=IntPtr.Zero) CloseHandle(process.thread);
                if(process.process!=IntPtr.Zero) CloseHandle(process.process);
                if(secret!=IntPtr.Zero) Marshal.ZeroFreeGlobalAllocUnicode(secret);
                if(descriptor!=IntPtr.Zero) LocalFree(descriptor);
            }
        }
    }
    public static void DeleteOwnedProfile(string sid) {
        Stage="profile_cleanup";
        Check(DeleteProfileW(sid,null,null));
    }
    public static void ReleaseObjects() {
        if(OwnershipUnresolved || (ClientLaunched && (!ClientExited || !JobQuiescent))) throw new InvalidOperationException("Live native objects retained");
        Stage="private_object_cleanup";
        if(desktop!=IntPtr.Zero) { Check(CloseDesktop(desktop)); desktop=IntPtr.Zero; }
        if(station!=IntPtr.Zero) { Check(CloseWindowStation(station)); station=IntPtr.Zero; }
        if(job!=IntPtr.Zero) { Check(CloseHandle(job)); job=IntPtr.Zero; }
    }
}
// No ordinary RegistryKey write/delete fallback is permitted. Registry reads,
// value writes/deletion, and conditional empty-key deletion share a KTM snapshot;
// any intervening non-transacted mutation aborts the transaction at commit.
public sealed class BroTypistInstallerPolicyTransaction : IDisposable {
    const string Path=@"Software\Policies\Microsoft\Windows\Installer";
    const string Parent=@"Software\Policies\Microsoft\Windows";
    static readonly IntPtr Hklm=new IntPtr(unchecked((int)0x80000002));
    const uint Access=0x3011f, View64=0x100;
    IntPtr transaction=IntPtr.Zero;
    bool committed;
    public Microsoft.Win32.RegistryKey Key { get; private set; }
    public bool Created { get; private set; }
    public bool Committed { get { return committed; } }
    [DllImport("KtmW32.dll", CharSet=CharSet.Unicode, ExactSpelling=true, SetLastError=true)]
    static extern IntPtr CreateTransaction(IntPtr attributes,IntPtr guid,uint options,uint isolation,uint flags,uint timeout,string description);
    [DllImport("KtmW32.dll", SetLastError=true)] static extern bool CommitTransaction(IntPtr transaction);
    [DllImport("KtmW32.dll", SetLastError=true)] static extern bool RollbackTransaction(IntPtr transaction);
    [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr handle);
    [DllImport("advapi32.dll", CharSet=CharSet.Unicode, ExactSpelling=true)]
    static extern int RegOpenKeyTransactedW(IntPtr root,string path,uint options,uint access,out IntPtr key,IntPtr transaction,IntPtr reserved);
    [DllImport("advapi32.dll", CharSet=CharSet.Unicode, ExactSpelling=true)]
    static extern int RegCreateKeyTransactedW(IntPtr root,string path,uint reserved,string kind,uint options,uint access,IntPtr attributes,out IntPtr key,out uint disposition,IntPtr transaction,IntPtr extended);
    [DllImport("advapi32.dll", CharSet=CharSet.Unicode, ExactSpelling=true)]
    static extern int RegDeleteKeyTransactedW(IntPtr root,string path,uint access,uint reserved,IntPtr transaction,IntPtr extended);
    [DllImport("advapi32.dll")] static extern int RegCloseKey(IntPtr key);
    static void Check(int error) { if(error!=0) throw new Win32Exception(error); }
    static Microsoft.Win32.RegistryKey Wrap(IntPtr handle) {
        return Microsoft.Win32.RegistryKey.FromHandle(new Microsoft.Win32.SafeHandles.SafeRegistryHandle(handle,true),Microsoft.Win32.RegistryView.Registry64);
    }
    public BroTypistInstallerPolicyTransaction() {
        transaction=CreateTransaction(IntPtr.Zero,IntPtr.Zero,0,0,0,30000,"BroTypist disposable CI DisableMSI fixture");
        if(transaction==IntPtr.Zero || transaction==new IntPtr(-1)) {
            transaction=IntPtr.Zero; throw new Win32Exception(Marshal.GetLastWin32Error());
        }
        try {
            IntPtr key; int result=RegOpenKeyTransactedW(Hklm,Path,0,Access,out key,transaction,IntPtr.Zero);
            if(result==2) return;
            Check(result); Key=Wrap(key);
        } catch { Dispose(); throw; }
    }
    public void CreateMissingKey() {
        if(Key!=null) throw new InvalidOperationException("Policy key is already open");
        IntPtr parent=IntPtr.Zero,key=IntPtr.Zero;
        try {
            // Require existing ancestors; creation is limited to Installer.
            Check(RegOpenKeyTransactedW(Hklm,Parent,0,0x2001f|View64,out parent,transaction,IntPtr.Zero));
            uint disposition;
            Check(RegCreateKeyTransactedW(parent,"Installer",0,null,0,Access,IntPtr.Zero,out key,out disposition,transaction,IntPtr.Zero));
            Key=Wrap(key); key=IntPtr.Zero; Created=disposition==1;
            if(!Created) throw new InvalidOperationException("Policy key appeared concurrently; transaction refused");
        } finally { if(key!=IntPtr.Zero) RegCloseKey(key); if(parent!=IntPtr.Zero) RegCloseKey(parent); }
    }
    public bool DeleteIfStillEmpty() {
        if(Key==null || Key.ValueCount!=0 || Key.SubKeyCount!=0) return false;
        Key.Dispose(); Key=null;
        Check(RegDeleteKeyTransactedW(Hklm,Path,View64,0,transaction,IntPtr.Zero));
        return true;
    }
    public void Commit() {
        if(!CommitTransaction(transaction)) throw new Win32Exception(Marshal.GetLastWin32Error());
        committed=true;
    }
    public void Dispose() {
        if(Key!=null) { Key.Dispose(); Key=null; }
        if(transaction!=IntPtr.Zero) {
            if(!committed) RollbackTransaction(transaction);
            CloseHandle(transaction); transaction=IntPtr.Zero;
        }
    }
}
'@
}
function Assert-RegularPath([string]$Path) {
    Require ([IO.Path]::IsPathFullyQualified($Path) -and $Path -cnotmatch '["\r\n]' -and -not $Path.EndsWith('\')) 'Unsafe absolute file path'
    $item = Get-Item -LiteralPath $Path -Force
    Require (-not $item.PSIsContainer) 'Regular file required'
    $ancestor = $item
    while ($null -ne $ancestor) {
        Require (($ancestor.Attributes -band [IO.FileAttributes]::ReparsePoint) -eq 0) 'Linked input or ancestor refused'
        $ancestor = if ($ancestor -is [IO.FileInfo]) { $ancestor.Directory } else { $ancestor.Parent }
    }
    return $item
}
function File-Identity([string]$Path) {
    $item = Assert-RegularPath $Path
    return @{ size = $item.Length; sha256 = (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant() }
}
function Copy-VerifiedFile([string]$Source, [string]$Destination, $Expected) {
    $before = File-Identity $Source
    Require ($before.size -eq $Expected.size -and $before.sha256 -ceq $Expected.sha256) 'Source bytes differ from expected identity'
    [IO.File]::Copy($Source, $Destination, $false)
    $after = File-Identity $Destination
    Require ($after.size -eq $Expected.size -and $after.sha256 -ceq $Expected.sha256) 'Staged bytes differ from expected identity'
    return @{ path = $Destination; size = $after.size; sha256 = $after.sha256 }
}
function Private-DirectoryAcl([string]$Supervisor, [string]$User, [string]$UserRights) {
    $acl = [Security.AccessControl.DirectorySecurity]::new()
    $acl.SetAccessRuleProtection($true, $false)
    foreach ($sid in @($Supervisor, 'S-1-5-18')) {
        $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new([Security.Principal.SecurityIdentifier]::new($sid), 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow'))
    }
    if (-not [string]::IsNullOrWhiteSpace($User)) {
        $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new([Security.Principal.SecurityIdentifier]::new($User), $UserRights, 'ContainerInherit,ObjectInherit', 'None', 'Allow'))
    }
    return $acl
}
function New-PrivateDirectory([string]$Path, $Acl) {
    Require (-not (Test-Path -LiteralPath $Path)) 'Existing private directory refused'
    [IO.FileSystemAclExtensions]::Create([IO.DirectoryInfo]::new($Path), $Acl)
}
function Write-NewJson([string]$Path, $Value) {
    $bytes = [Text.UTF8Encoding]::new($false).GetBytes(($Value | ConvertTo-Json -Depth 32) + "`n")
    $stream = [IO.File]::Open($Path, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
    try { $stream.Write($bytes, 0, $bytes.Length); $stream.Flush($true) } finally { $stream.Dispose() }
}
function Validate-ReportDirectory([string]$Path) {
    Require (-not [string]::IsNullOrWhiteSpace($Path) -and [IO.Path]::IsPathFullyQualified($Path)) 'Existing absolute ReportDirectory required'
    $fullPath = [IO.Path]::GetFullPath($Path)
    $relative = [IO.Path]::GetRelativePath([IO.Path]::GetFullPath($env:RUNNER_TEMP), $fullPath)
    Require ($relative -ne '.' -and $relative -ne '..' -and -not $relative.StartsWith('..\') -and -not [IO.Path]::IsPathRooted($relative)) 'ReportDirectory must remain beneath RUNNER_TEMP'
    $directory = Get-Item -LiteralPath $fullPath -Force
    Require $directory.PSIsContainer 'Existing ReportDirectory required'
    while ($null -ne $directory) {
        Require (($directory.Attributes -band [IO.FileAttributes]::ReparsePoint) -eq 0) 'Linked report directory or ancestor refused'
        $directory = $directory.Parent
    }
    Require (-not (Test-Path -LiteralPath (Join-Path $fullPath 'client-observations.json'))) 'Existing client evidence refused'
    return $fullPath
}
function Save-ClientReceipt {
    Require $reportDirectoryValidated 'Public report directory was not validated'
    $json = $metadata | ConvertTo-Json -Depth 32
    Require ([Text.Encoding]::UTF8.GetByteCount($json) -le 65536) 'Client receipt exceeds bounded metadata limit'
    Write-NewJson (Join-Path $ReportDirectory 'client-observations.json') $metadata
}
function Failure-Facts([Management.Automation.ErrorRecord]$ErrorRecord, [string]$Stage) {
    $exceptions = [Collections.Generic.List[object]]::new()
    $exception = $ErrorRecord.Exception
    while ($null -ne $exception -and $exceptions.Count -lt 4) {
        $exceptions.Add(@{
            type = $exception.GetType().FullName; hresult = $exception.HResult
            native_error = if ($exception -is [ComponentModel.Win32Exception]) { $exception.NativeErrorCode } else { $null }
        })
        $exception = $exception.InnerException
    }
    $stack = [string]$ErrorRecord.ScriptStackTrace
    if ($stack.Length -gt 4096) { $stack = $stack.Substring(0, 4096) }
    $lines = @([regex]::Matches($stack, ': line (\d+)') | Select-Object -First 8 | ForEach-Object { [int]$_.Groups[1].Value })
    # Classification and source lines only: no exception text, paths or identities.
    return @{ stage = $Stage; category = $ErrorRecord.CategoryInfo.Category.ToString(); source_lines = $lines; exceptions = @($exceptions.ToArray()) }
}
function Stop-OwnedVm([string]$Reason) {
    $metadata.passed = $false
    $metadata.classification = 'unresolved'
    $metadata.teardown_required = $true
    $metadata.diagnostics.Add($Reason)
    try { Save-ClientReceipt } catch { [Console]::Error.WriteLine('Fail-closed public client receipt unavailable') }
    # Only parent-owned bounded facts are published. Never read/promote the
    # private tree or release the policy/account/profile lease on this path.
    [Console]::Error.WriteLine((@{
        stage = $metadata.stage; classification = $metadata.classification
        failure = $metadata.failure; cleanup_failure = $metadata.cleanup_failure
        quiescence_verified = $metadata.quiescence_verified
        uninstall_verified = $metadata.uninstall_verified
        restoration_verified = $metadata.restoration_verified
        teardown_required = $metadata.teardown_required
    } | ConvertTo-Json -Depth 8 -Compress))
    exit 1
}
function Read-BootstrapFailure {
    $path = Join-Path $coreWork 'bootstrap-failure.json'
    if (-not (Test-Path -LiteralPath $path)) { return }
    $file = Assert-RegularPath $path
    Require ($file.Length -gt 0 -and $file.Length -le 8192) 'Bootstrap failure receipt size refused'
    $receipt = [IO.File]::ReadAllText($path) | ConvertFrom-Json
    Require ($receipt.passed -is [bool] -and -not $receipt.passed -and $receipt.schema -eq 1) 'Bootstrap failure classification differs'
    $facts = $receipt.failure
    Require (@('bootstrap_identity', 'bootstrap_staging', 'bootstrap_core') -ccontains $facts.stage) 'Bootstrap failure stage differs'
    Require (([Enum]::GetNames([Management.Automation.ErrorCategory])) -ccontains $facts.category) 'Bootstrap failure category differs'
    $lines = @($facts.source_lines)
    Require ($lines.Count -le 8) 'Bootstrap failure line count refused'
    foreach ($line in $lines) { Require (($line -is [int] -or $line -is [long]) -and $line -gt 0 -and $line -le 100000) 'Bootstrap failure source line refused' }
    $exceptions = @($facts.exceptions)
    Require ($exceptions.Count -le 4) 'Bootstrap failure exception count refused'
    $safeExceptions = @(
        foreach ($exception in $exceptions) {
            Require (($exception.hresult -is [int] -or $exception.hresult -is [long]) -and $exception.hresult -ge [int]::MinValue -and $exception.hresult -le [int]::MaxValue) 'Bootstrap failure HRESULT refused'
            Require ($null -eq $exception.native_error -or (($exception.native_error -is [int] -or $exception.native_error -is [long]) -and $exception.native_error -ge [int]::MinValue -and $exception.native_error -le [int]::MaxValue)) 'Bootstrap failure native error refused'
            $type = if (@('System.Management.Automation.RuntimeException', 'System.Management.Automation.MethodInvocationException', 'System.Management.Automation.ActionPreferenceStopException', 'System.ComponentModel.Win32Exception', 'System.InvalidOperationException', 'System.UnauthorizedAccessException', 'System.IO.IOException', 'System.IO.FileNotFoundException', 'System.Security.SecurityException') -ccontains $exception.type) { $exception.type } else { 'UnclassifiedException' }
            @{ type = $type; hresult = $exception.hresult; native_error = $exception.native_error }
        }
    )
    # Diagnostic classification only: never evidence of token, MSI, uninstall,
    # or cleanup success, and never publish arbitrary child-authored strings.
    $metadata.bootstrap_failure = @{
        passed = $false; diagnostic_only = $true
        failure = @{ stage = $facts.stage; category = $facts.category; source_lines = $lines; exceptions = $safeExceptions }
    }
}
function Observe-VerifiedCoreIdentity($Outcome) {
    $proofs = @($Outcome.observations | Where-Object { $_.check -ceq 'genuine_standard_user_verified_before_installer' })
    if ($proofs.Count -eq 0) { return }
    Require ($proofs.Count -eq 1 -and $proofs[0].passed -is [bool] -and $proofs[0].passed) 'Ambiguous or failed native standard-user proof'
    $evidence = $proofs[0].evidence
    Require ($evidence.profile_matches_logon -is [bool] -and $evidence.profile_matches_logon) 'Native profile proof missing'
    $actual = [ordered]@{}
    foreach ($name in @('client', 'process_primary_token')) {
        $token = $evidence.$name
        $facts = [ordered]@{}
        foreach ($field in @('matches_expected_user', 'is_system', 'administrator_role_enabled', 'administrator_group_present')) {
            Require ($token.$field -is [bool] -and $token.$field -eq ($field -ceq 'matches_expected_user')) 'Native account token proof differs'
            $facts[$field] = $token.$field
        }
        foreach ($field in @('token_elevation', 'elevation_type', 'integrity_rid', 'token_has_restrictions', 'token_type')) {
            Require (($token.$field -is [long] -or $token.$field -is [int]) -and $token.$field -ge 0 -and $token.$field -le [uint32]::MaxValue) 'Native token fact type differs'
            $facts[$field] = $token.$field
        }
        Require ($facts.token_elevation -eq 0 -and $facts.integrity_rid -eq 8192 -and $facts.token_type -eq 1 -and $token.impersonation_level -ceq 'None') 'Native primary token proof differs'
        $facts.impersonation_level = 'None'
        $actual[$name] = $facts
    }
    $metadata.actual_standard_user_token = $actual
    $metadata.genuine_standard_user = $true
}
function Promote-Report([string]$Relative, [bool]$Required) {
    $source = Join-Path $coreWork $Relative
    if (-not (Test-Path -LiteralPath $source)) {
        Require (-not $Required) 'Required native report missing'
        return
    }
    $file = Assert-RegularPath $source
    Require ($file.Length -gt 0 -and $file.Length -le 262144) 'Native report size refused'
    $text = [IO.File]::ReadAllText($source)
    [void]($text | ConvertFrom-Json)
    # Do not publish profile/account identifiers even when a native diagnostic
    # includes an unexpected Windows path. JSON escaping is redacted as well.
    foreach ($private in @($ownedRoot, $ownedProfilePath, $accountName, $validationSid, $supervisorSid, $env:USERPROFILE)) {
        if (-not [string]::IsNullOrWhiteSpace($private)) {
            $text = $text.Replace($private.Replace('\', '\\'), '<private>').Replace($private, '<private>')
        }
    }
    $destination = Join-Path $Work $Relative
    $parent = [IO.Path]::GetDirectoryName($destination)
    if ($parent -ne $Work) {
        Require (-not (Test-Path -LiteralPath $parent)) 'Existing promoted report directory refused'
        [void][IO.Directory]::CreateDirectory($parent)
    }
    $bytes = [Text.UTF8Encoding]::new($false).GetBytes($text)
    $stream = [IO.File]::Open($destination, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
    try { $stream.Write($bytes, 0, $bytes.Length); $stream.Flush($true) } finally { $stream.Dispose() }
}
function Remove-OwnedTree([IO.DirectoryInfo]$Directory) {
    Require (($Directory.Attributes -band [IO.FileAttributes]::ReparsePoint) -eq 0) 'Linked private directory refused during cleanup'
    foreach ($entry in $Directory.EnumerateFileSystemInfos()) {
        Require (($entry.Attributes -band [IO.FileAttributes]::ReparsePoint) -eq 0) 'Linked private entry refused during cleanup'
        if ($entry -is [IO.DirectoryInfo]) { Remove-OwnedTree $entry }
        else { $entry.Delete() }
    }
    $Directory.Delete()
}
function Remove-CreatedTree([string]$Path, [bool]$Created) {
    if ([string]::IsNullOrWhiteSpace($Path)) {
        Require (-not $Created) 'Created tree has no owned path'
        return
    }
    if (Test-Path -LiteralPath $Path) {
        Require $Created 'Unconfirmed staging-directory creation; refusing deletion'
        Remove-OwnedTree ([IO.DirectoryInfo]::new($Path))
    }
    Require (-not (Test-Path -LiteralPath $Path)) 'Owned staging tree remains'
}
function Invoke-StandardUserCore([string]$Executable, [string]$Client, [string]$InputPath, [string]$CoreWork, [string]$Hash) {
    foreach ($path in @($Executable, $Client, $InputPath, $CoreWork)) {
        Require ([IO.Path]::IsPathFullyQualified($path) -and $path -cnotmatch '["\r\n]' -and -not $path.EndsWith('\')) 'Unsafe standard-user child path'
    }
    $command = '"{0}" -NoLogo -NoProfile -NonInteractive -File "{1}" -InputFile "{2}" -Work "{3}" -Bootstrap -HandoffHash "{4}"' -f $Executable, $Client, $InputPath, $CoreWork, $Hash
    Require ($command.Length -lt 1024) 'Native logon command length refused'
    try { return [BroTypistStandardUserInstallerClient]::Run($Executable, $command, $CoreWork, $accountName, $password, $validationSid, $supervisorSid) }
    finally {
        $metadata.child_deadline_exceeded = [BroTypistStandardUserInstallerClient]::DeadlineExceeded
        $metadata.native_error_code = if ($null -ne [BroTypistStandardUserInstallerClient]::FailureStage) { [BroTypistStandardUserInstallerClient]::FailureNativeError } else { [BroTypistStandardUserInstallerClient]::LastNativeError }
        $metadata.native_stage = if ($null -ne [BroTypistStandardUserInstallerClient]::FailureStage) { [BroTypistStandardUserInstallerClient]::FailureStage } else { [BroTypistStandardUserInstallerClient]::Stage }
        $metadata.child_exit_confirmed = [BroTypistStandardUserInstallerClient]::ClientExited
        $metadata.native_ownership_unresolved = [BroTypistStandardUserInstallerClient]::OwnershipUnresolved
        $metadata.native_cleanup_failure_stage = [BroTypistStandardUserInstallerClient]::CleanupFailureStage
        $metadata.native_cleanup_error_code = [BroTypistStandardUserInstallerClient]::CleanupNativeError
    }
}

if ($Bootstrap) {
    # This branch never creates accounts or touches host policy. Its immutable
    # handoff is authenticated by argv hash, not by inherited CI environment.
    $bootstrapHandoffAuthenticated = $false
    $bootstrapStage = 'bootstrap_identity'
    try {
        Require ($IsWindows -and $HandoffHash -cmatch '^[0-9a-f]{64}$') 'Invalid bootstrap platform or handoff'
        $handoffPath = Join-Path $PSScriptRoot 'handoff.json'
        $handoffIdentity = File-Identity $handoffPath
        Require ($handoffIdentity.size -le 65536 -and $handoffIdentity.sha256 -ceq $HandoffHash) 'Bootstrap handoff identity differs'
        $handoff = Get-Content -LiteralPath $handoffPath -Raw | ConvertFrom-Json
        Require ($InputFile -ceq $handoff.input -and $Work -ceq $handoff.work) 'Bootstrap path handoff differs'
        $bootstrapHandoffAuthenticated = $true
        $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
        try {
            Require ($identity.User.Value -ceq $handoff.expected_user_sid -and -not $identity.IsSystem -and -not [Security.Principal.WindowsPrincipal]::new($identity).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) 'Bootstrap account differs'
        } finally { $identity.Dispose() }
        $bootstrapStage = 'bootstrap_staging'
        foreach ($file in $handoff.files) {
            $actual = File-Identity $file.path
            Require ($actual.size -eq $file.size -and $actual.sha256 -ceq $file.sha256) 'Bootstrap staged file identity differs'
        }
        $env:CI = 'true'; $env:GITHUB_ACTIONS = 'true'
        $env:RUNNER_ENVIRONMENT = 'github-hosted'; $env:RUNNER_OS = 'Windows'
        $env:BROTYPIST_INSTALLER_SMOKE_DISPOSABLE = '1'; $env:RUNNER_TEMP = $handoff.root
        $bootstrapStage = 'bootstrap_core'
        & $handoff.core -InputFile $InputFile -Work $Work -RequireStandardUser -ExpectedUserSid $handoff.expected_user_sid -SupervisorSid $handoff.supervisor_sid
        exit $LASTEXITCODE
    } catch {
        $bootstrapFailure = Failure-Facts $_ $bootstrapStage
        if ($bootstrapHandoffAuthenticated) {
            try {
                $bootstrapReceipt = @{ schema = 1; passed = $false; failure = $bootstrapFailure }
                Require ([Text.Encoding]::UTF8.GetByteCount(($bootstrapReceipt | ConvertTo-Json -Depth 32)) -lt 8192) 'Bootstrap failure metadata exceeds bound'
                Write-NewJson (Join-Path $Work 'bootstrap-failure.json') $bootstrapReceipt
            } catch { [Console]::Error.WriteLine('Bounded bootstrap failure receipt unavailable') }
        }
        [Console]::Error.WriteLine('Standard-user bootstrap failed; diagnostic facts do not authorize supervisor cleanup')
        exit 1
    }
}

try {
    Require ($IsWindows -and $env:CI -ceq 'true' -and $env:GITHUB_ACTIONS -ceq 'true' -and $env:RUNNER_ENVIRONMENT -ceq 'github-hosted' -and $env:RUNNER_OS -ceq 'Windows' -and $env:BROTYPIST_INSTALLER_SMOKE_DISPOSABLE -ceq '1') 'Disposable hosted Windows CI guard required'
    Require ([Runtime.InteropServices.RuntimeInformation]::OSArchitecture -eq [Runtime.InteropServices.Architecture]::X64) 'Native Windows x86_64 required'
    $ReportDirectory = Validate-ReportDirectory $ReportDirectory
    $reportDirectoryValidated = $true
    foreach ($path in @($InputFile, $Work)) {
        Require ([IO.Path]::IsPathFullyQualified($path)) 'Absolute private probe paths required'
        $relative = [IO.Path]::GetRelativePath([IO.Path]::GetFullPath($env:RUNNER_TEMP), [IO.Path]::GetFullPath($path))
        Require ($relative -ne '.' -and $relative -ne '..' -and -not $relative.StartsWith('..\') -and -not [IO.Path]::IsPathRooted($relative)) 'Probe paths must remain beneath RUNNER_TEMP'
        $ancestor = if (Test-Path -LiteralPath $path -PathType Container) { [IO.DirectoryInfo]::new($path) } else { [IO.FileInfo]::new($path) }
        while ($null -ne $ancestor) {
            if ($ancestor.Exists) { Require (($ancestor.Attributes -band [IO.FileAttributes]::ReparsePoint) -eq 0) 'Linked probe ancestor refused' }
            $ancestor = if ($ancestor -is [IO.FileInfo]) { $ancestor.Directory } else { $ancestor.Parent }
        }
    }
    $metadata.stage = 'native_initialization'
    Initialize-InstallerNative
    $nativeInitialized = $true
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    try {
        Require (-not $identity.IsSystem -and [Security.Principal.WindowsPrincipal]::new($identity).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) 'Administrative non-SYSTEM CI supervisor required'
        $supervisorSid = $identity.User.Value
    } finally { $identity.Dispose() }
    $metadata.stage = 'account_creation'
    $accountName = 'btci-' + [Convert]::ToHexString([Security.Cryptography.RandomNumberGenerator]::GetBytes(6)).ToLowerInvariant()
    Require (@(Get-LocalUser | Where-Object Name -CEQ $accountName).Count -eq 0) 'Fresh account name collision refused'
    $password = [Security.SecureString]::new()
    foreach ($character in 'Aa1!'.ToCharArray()) { $password.AppendChar($character) }
    $randomPasswordBytes = [Security.Cryptography.RandomNumberGenerator]::GetBytes(48)
    try {
        $alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_'
        foreach ($value in $randomPasswordBytes) { $password.AppendChar($alphabet[$value -band 63]) }
    } finally { [Array]::Clear($randomPasswordBytes, 0, $randomPasswordBytes.Length); $randomPasswordBytes = $null }
    $password.MakeReadOnly()
    $accountCreationAttempted = $true
    $account = New-LocalUser -Name $accountName -Password $password -Description 'Disposable BroTypist CI validation only' -AccountNeverExpires
    $validationSid = $account.SID.Value
    Require ($validationSid -cne $supervisorSid -and $validationSid -cmatch '^S-1-5-21-\d+-\d+-\d+-\d+$') 'Fresh local account SID required'
    $profileKeyPath = "SOFTWARE\Microsoft\Windows NT\CurrentVersion\ProfileList\$validationSid"
    $profileBase = [Microsoft.Win32.RegistryKey]::OpenBaseKey([Microsoft.Win32.RegistryHive]::LocalMachine, [Microsoft.Win32.RegistryView]::Registry64)
    try {
        $existingProfile = $profileBase.OpenSubKey($profileKeyPath)
        try { Require ($null -eq $existingProfile) 'Existing profile for fresh account refused' } finally { if ($null -ne $existingProfile) { $existingProfile.Dispose() } }
        $profileWasAbsent = $true
    } finally { $profileBase.Dispose() }
    $users = Get-LocalGroup -SID ([Security.Principal.SecurityIdentifier]::new('S-1-5-32-545'))
    if (@(Get-LocalGroupMember -Group $users | Where-Object { $_.SID.Value -ceq $validationSid }).Count -eq 0) {
        Add-LocalGroupMember -Group $users -Member $account
    }
    foreach ($group in Get-LocalGroup) {
        if (@(Get-LocalGroupMember -Group $group | Where-Object { $_.SID.Value -ceq $validationSid }).Count -gt 0) {
            Require ($group.SID.Value -ceq 'S-1-5-32-545') 'Fresh account has membership beyond Users'
        }
    }
    $metadata.stage = 'private_staging'
    $ownedRoot = Join-Path $env:RUNNER_TEMP ('btci-' + [Guid]::NewGuid().ToString('N'))
    New-PrivateDirectory $ownedRoot (Private-DirectoryAcl $supervisorSid '' '')
    $ownedRootCreated = $true
    $stagedInputs = Join-Path $ownedRoot 'inputs'
    New-PrivateDirectory $stagedInputs (Private-DirectoryAcl $supervisorSid $validationSid 'ReadAndExecute')
    $coreWork = Join-Path $ownedRoot 'work'
    New-PrivateDirectory $coreWork (Private-DirectoryAcl $supervisorSid $validationSid 'Modify')
    $input = Get-Content -LiteralPath $InputFile -Raw | ConvertFrom-Json
    $manifest = [Collections.Generic.List[object]]::new()
    foreach ($which in @('package', 'previous')) {
        $package = $input.$which
        if ($null -eq $package) { continue }
        foreach ($kind in @('msi', 'zip')) {
            $artifact = $package.artifacts.$kind
            $staged = Copy-VerifiedFile $artifact.path (Join-Path $stagedInputs "$which.$kind") $artifact
            $manifest.Add($staged); $artifact.path = $staged.path
        }
    }
    foreach ($optional in @('worker', 'model')) {
        if (-not [string]::IsNullOrWhiteSpace($input.$optional)) {
            $source = [string]$input.$optional
            $suffix = if ($optional -ceq 'worker') { '.exe' } else { '.bin' }
            $staged = Copy-VerifiedFile $source (Join-Path $stagedInputs ($optional + $suffix)) (File-Identity $source)
            $manifest.Add($staged); $input.$optional = $staged.path
        }
    }
    foreach ($script in @('windows-installer-client.ps1', 'windows-installer-smoke.ps1')) {
        $source = Join-Path $PSScriptRoot $script
        $manifest.Add((Copy-VerifiedFile $source (Join-Path $stagedInputs $script) (File-Identity $source)))
    }
    $stagedInput = Join-Path $stagedInputs 'input.json'
    Write-NewJson $stagedInput $input
    $stagedIdentity = File-Identity $stagedInput
    $manifest.Add(@{ path = $stagedInput; size = $stagedIdentity.size; sha256 = $stagedIdentity.sha256 })
    $core = Join-Path $stagedInputs 'windows-installer-smoke.ps1'
    $metadata.core_sha256 = (File-Identity $core).sha256
    $handoffPath = Join-Path $stagedInputs 'handoff.json'
    Write-NewJson $handoffPath @{ root = $ownedRoot; input = $stagedInput; work = $coreWork; core = $core; expected_user_sid = $validationSid; supervisor_sid = $supervisorSid; files = @($manifest.ToArray()) }
    $handoffHash = (File-Identity $handoffPath).sha256
    Set-Acl -LiteralPath $ownedRoot -AclObject (Private-DirectoryAcl $supervisorSid $validationSid 'ReadAndExecute')
    $metadata.staged_file_count = $manifest.Count
    $metadata.staged_bytes_verified = $true
    $metadata.msi_system_read = 'owned staged MSI only; original artifact ACLs unchanged'
    $metadata.stage = 'policy_fixture'
    $base = [Microsoft.Win32.RegistryKey]::OpenBaseKey([Microsoft.Win32.RegistryHive]::LocalMachine, [Microsoft.Win32.RegistryView]::Registry64)
    $apply = [BroTypistInstallerPolicyTransaction]::new()
    try {
        $metadata.before = Policy-Snapshot $apply.Key
        $policy = $metadata.before.values.DisableMSI
        Assert-DisableMsiPolicy $policy
        $valueWasAbsent = -not $policy.present
        if ($valueWasAbsent) {
            if ($null -eq $apply.Key) { $apply.CreateMissingKey() }
            # Reads and this write share the same transacted key handle.
            # Concurrent ordinary or transacted mutations conflict at commit.
            $apply.Key.SetValue('DisableMSI', [int]0, [Microsoft.Win32.RegistryValueKind]::DWord)
        }
        $metadata.transaction_intended_during = Policy-Snapshot $apply.Key
        $apply.Commit()
    } finally {
        # Recover committed ownership even if PowerShell is interrupted between
        # the native commit and the next script statement.
        if ($apply.Committed) {
            $createdValue = $valueWasAbsent; $createdKey = $apply.Created
            $metadata.policy_modified = $createdValue; $metadata.policy_key_created = $createdKey
        }
        $apply.Dispose()
    }
    $key = $base.OpenSubKey($policyPath, $false)
    $metadata.during = Policy-Snapshot $key
    Require ($metadata.during.values.DisableMSI.kind -ceq 'DWord' -and $metadata.during.values.DisableMSI.value -eq 0) 'Temporary DisableMSI fixture was not applied'
    $metadata.stage = 'standard_user_launch'
    $nativeExit = Invoke-StandardUserCore (Get-Process -Id $PID).Path (Join-Path $stagedInputs 'windows-installer-client.ps1') $stagedInput $coreWork $handoffHash
    $metadata.native_exit_code = $nativeExit
    Require ($nativeExit -eq 0) 'Standard-user native lifecycle failed'
    $nativeLifecycleSucceeded = $true
} catch {
    $metadata.failure = Failure-Facts $_ $metadata.stage
    $metadata.diagnostics.Add(('Stage {0} failed ({1}); native observations and typed policy snapshots are authoritative' -f $metadata.stage, $_.Exception.GetType().Name))
    try { if ($null -ne $base) { $metadata.during = Policy-Snapshot $key } }
    catch { $metadata.diagnostics.Add('During-policy snapshot unavailable; never counted as passed') }
    $metadata.passed = $false
} finally {
    try {
    # A bounded native failure never authorizes cleanup. Unknown process/MSI
    # ownership or uninstall leaves every remaining lease for hosted-VM teardown.
    $quiescent = -not $nativeInitialized -or (-not [BroTypistStandardUserInstallerClient]::ClientLaunched -and -not [BroTypistStandardUserInstallerClient]::OwnershipUnresolved)
    $uninstallVerified = $quiescent
    if ($nativeInitialized -and -not [BroTypistStandardUserInstallerClient]::OwnershipUnresolved -and [BroTypistStandardUserInstallerClient]::ClientLaunched -and [BroTypistStandardUserInstallerClient]::ClientExited -and [BroTypistStandardUserInstallerClient]::JobQuiescent) {
        if (-not [BroTypistStandardUserInstallerClient]::ClientResumed) {
            $quiescent = $true; $uninstallVerified = $true
        } else {
            # The child and its entire job have exited. This bounded failure
            # classification cannot replace the core's MSI/uninstall receipt.
            try { Read-BootstrapFailure }
            catch { $metadata.bootstrap_failure_read_error = Failure-Facts $_ 'bootstrap_receipt' }
            try {
                $outcomePath = Join-Path $coreWork 'observations.json'
                $file = Assert-RegularPath $outcomePath
                Require ($file.Length -gt 0 -and $file.Length -le 262144) 'Native quiescence receipt missing/oversized'
                $outcome = Get-Content -LiteralPath $outcomePath -Raw | ConvertFrom-Json
                Observe-VerifiedCoreIdentity $outcome
                Assert-NativeQuiescence $outcome
                $quiescent = $true
                $uninstallVerified = $outcome.quiescence.uninstall_verified -is [bool] -and $outcome.quiescence.uninstall_verified
            } catch {
                $quiescent = $false
                $metadata.cleanup_failure = Failure-Facts $_ 'native_receipt'
            }
        }
    }
    $metadata.quiescence_verified = $quiescent
    $metadata.uninstall_verified = $uninstallVerified
    if (-not $quiescent) { Stop-OwnedVm 'Native quiescence unknown; no restoration, deletion or report promotion; disposable VM teardown required' }
    if (-not $uninstallVerified) { Stop-OwnedVm 'Uninstall unverified; no restoration, deletion or report promotion; disposable VM teardown required' }
    if ($null -ne $outcome) {
        $metadata.stage = 'report_promotion'
        # Read this new SID's registration solely to redact completed reports.
        # Actual profile/account cleanup independently revalidates ownership.
        Require ($null -ne $account -and $profileWasAbsent) 'Owned profile baseline required for report redaction'
        Require ((Get-LocalUser -Name $accountName).SID.Value -ceq $validationSid) 'Owned account SID changed before report redaction'
        $redactionBase = [Microsoft.Win32.RegistryKey]::OpenBaseKey([Microsoft.Win32.RegistryHive]::LocalMachine, [Microsoft.Win32.RegistryView]::Registry64)
        try {
            $redactionKey = $redactionBase.OpenSubKey($profileKeyPath)
            try {
                if ($null -ne $redactionKey) {
                    $registeredPath = [string]$redactionKey.GetValue('ProfileImagePath')
                    if (-not [string]::IsNullOrWhiteSpace($registeredPath)) {
                        Require ([IO.Path]::IsPathFullyQualified($registeredPath)) 'Registered redaction profile path must be absolute'
                        $ownedProfilePath = [IO.Path]::GetFullPath($registeredPath)
                    }
                }
            } finally { if ($null -ne $redactionKey) { $redactionKey.Dispose() } }
        } finally { $redactionBase.Dispose() }
        Promote-Report 'observations.json' $true
        Promote-Report 'tiny-worker/worker-smoke.json' $false
        Promote-Report 'portable-tiny-worker/worker-smoke.json' $false
    }
    $metadata.stage = 'policy_restoration'
    try {
        if ($createdValue) {
            if ($null -ne $key) { $key.Dispose(); $key = $null }
            $restore = [BroTypistInstallerPolicyTransaction]::new()
            try {
                $current = Policy-Snapshot $restore.Key
                Require (-not $current.values.DisableMSI.present -or ($current.values.DisableMSI.kind -ceq 'DWord' -and $current.values.DisableMSI.value -eq 0)) 'Concurrent DisableMSI change preserved; restoration unresolved'
                if ($current.values.DisableMSI.present) { $restore.Key.DeleteValue('DisableMSI', $false) }
                # Empty-key read and nonrecursive deletion are part of this same
                # conflict-detecting transaction; concurrent values survive.
                if ($createdKey) { $metadata.empty_created_key_deleted = $restore.DeleteIfStillEmpty() }
                $restore.Commit()
            } finally { $restore.Dispose() }
        }
        if ($null -ne $key) { $key.Dispose(); $key = $null }
        if ($null -ne $base) {
            $key = $base.OpenSubKey($policyPath, $false)
            $metadata.restored = Policy-Snapshot $key
            foreach ($name in @('DisableMSI', 'DisableUserInstalls', 'AlwaysInstallElevated')) {
                foreach ($field in @('present', 'kind', 'value', 'value_sha256')) {
                    Require ($metadata.restored.values[$name][$field] -ceq $metadata.before.values[$name][$field]) 'Policy restoration or unchanged companion-policy verification failed'
                }
            }
            if (-not $createdKey) { Require ($metadata.restored.key_present -eq $metadata.before.key_present) 'Original policy key presence changed' }
            $metadata.restoration_verified = $true
        }
    } catch {
        $metadata.passed = $false
        $metadata.cleanup_failure = Failure-Facts $_ $metadata.stage
        $metadata.diagnostics.Add('Policy restoration could not be verified; inspect before/during/restored snapshots; fixture does not count as passed')
        try {
            if ($null -ne $base) {
                if ($null -ne $key) { $key.Dispose(); $key = $null }
                $key = $base.OpenSubKey($policyPath, $false)
                $metadata.restored = Policy-Snapshot $key
            }
        } catch { $metadata.diagnostics.Add('Restored policy snapshot itself was unavailable; restoration remains unverified') }
    } finally {
        if ($null -ne $key) { $key.Dispose() }
        if ($null -ne $base) { $base.Dispose() }
    }
    if ($createdValue -and -not $metadata.restoration_verified) { Stop-OwnedVm 'Owned policy restoration unresolved; remaining resources require disposable VM teardown' }
    try {
        $metadata.stage = 'profile_cleanup'
        if ($null -ne $account) {
            Require ((Get-LocalUser -Name $accountName).SID.Value -ceq $validationSid) 'Owned account SID changed before profile cleanup'
            Require $profileWasAbsent 'Profile absence before logon not established'
            $profileBase = [Microsoft.Win32.RegistryKey]::OpenBaseKey([Microsoft.Win32.RegistryHive]::LocalMachine, [Microsoft.Win32.RegistryView]::Registry64)
            try {
                $profileKey = $profileBase.OpenSubKey($profileKeyPath)
                $profilePath = $null
                try {
                    if ($null -ne $profileKey) { $profilePath = [string]$profileKey.GetValue('ProfileImagePath') }
                } finally { if ($null -ne $profileKey) { $profileKey.Dispose() } }
                if ([BroTypistStandardUserInstallerClient]::LogonAttempted) {
                    Require (-not [string]::IsNullOrWhiteSpace($profilePath)) 'Logon profile registration unavailable; disposition unknown'
                }
                if ($null -ne $profilePath) {
                    Require ([IO.Path]::IsPathFullyQualified($profilePath) -and -not [string]::IsNullOrWhiteSpace($profilePath)) 'Owned profile path unavailable'
                    Require ([BroTypistStandardUserInstallerClient]::LogonAttempted) 'Unexpected profile registration before owned logon'
                    $ownedProfilePath = [IO.Path]::GetFullPath($profilePath)
                    Require ($ownedProfilePath -ine [IO.Path]::GetPathRoot($ownedProfilePath) -and $ownedProfilePath -ine [IO.Path]::GetFullPath($env:USERPROFILE)) 'Protected profile path refused'
                    $profileAncestor = [IO.DirectoryInfo]::new($ownedProfilePath)
                    while ($null -ne $profileAncestor) {
                        if ($profileAncestor.Exists) { Require (($profileAncestor.Attributes -band [IO.FileAttributes]::ReparsePoint) -eq 0) 'Linked profile path refused' }
                        $profileAncestor = $profileAncestor.Parent
                    }
                    # CPWL's real profile can outlive process exit briefly.
                    # Wait only for this exact newly-created SID's hive closure.
                    $profileWait = [Diagnostics.Stopwatch]::StartNew()
                    while ((Test-Path -LiteralPath "Registry::HKEY_USERS\$validationSid") -or (Test-Path -LiteralPath "Registry::HKEY_USERS\${validationSid}_Classes")) {
                        Require ($profileWait.ElapsedMilliseconds -lt 30000) 'Owned profile hive closure deadline exceeded'
                        [Threading.Thread]::Sleep(100)
                    }
                    # DeleteProfile resolves the registered profile by our owned
                    # SID; no caller-supplied path or recursive profile deletion.
                    [BroTypistStandardUserInstallerClient]::DeleteOwnedProfile($validationSid)
                    Require (-not (Test-Path -LiteralPath $profilePath)) 'Owned profile directory remains after lifecycle deletion'
                }
                $remainingProfile = $profileBase.OpenSubKey($profileKeyPath)
                try { Require ($null -eq $remainingProfile) 'Owned profile registration remains' }
                finally { if ($null -ne $remainingProfile) { $remainingProfile.Dispose() } }
                Require (-not (Test-Path -LiteralPath "Registry::HKEY_USERS\$validationSid") -and -not (Test-Path -LiteralPath "Registry::HKEY_USERS\${validationSid}_Classes")) 'Owned profile hive remains loaded'
            } finally { $profileBase.Dispose() }
            $metadata.profile_cleanup_verified = $true
            $metadata.stage = 'account_cleanup'
            Require ((Get-LocalUser -Name $accountName).SID.Value -ceq $validationSid) 'Owned account SID changed before deletion'
            Remove-LocalUser -SID ([Security.Principal.SecurityIdentifier]::new($validationSid))
            Require (@(Get-LocalUser | Where-Object { $_.SID.Value -ceq $validationSid -or $_.Name -ceq $accountName }).Count -eq 0) 'Owned account remains'
        } else {
            Require (-not $accountCreationAttempted) 'Account creation result unknown; account ownership retained'
            $metadata.profile_cleanup_verified = $true
        }
        $metadata.account_cleanup_verified = $true
        if ($nativeInitialized) { [BroTypistStandardUserInstallerClient]::ReleaseObjects() }
        $metadata.stage = 'private_tree_cleanup'
        Remove-CreatedTree $ownedRoot $ownedRootCreated
        $metadata.private_tree_cleanup_verified = $true
    } catch {
        if ($null -eq $metadata.cleanup_failure) { $metadata.cleanup_failure = Failure-Facts $_ $metadata.stage }
        $metadata.diagnostics.Add(('Cleanup stage {0} failed ({1}); no exception text or identifiers retained' -f $metadata.stage, $_.Exception.GetType().Name))
        Stop-OwnedVm 'Owned account/profile/tree cleanup unverified; disposable VM teardown required'
    }
    $metadata.passed = $nativeLifecycleSucceeded -and $null -eq $metadata.failure -and $null -eq $metadata.cleanup_failure -and $null -ne $outcome -and $outcome.passed -is [bool] -and $outcome.passed -and $metadata.restoration_verified -and $metadata.genuine_standard_user -and $metadata.account_cleanup_verified -and $metadata.profile_cleanup_verified -and $metadata.private_tree_cleanup_verified
    $metadata.stage = 'complete'
    } catch {
        if ($null -eq $metadata.cleanup_failure) { $metadata.cleanup_failure = Failure-Facts $_ $metadata.stage }
        Stop-OwnedVm 'Secondary cleanup failure; original failure preserved; disposable VM teardown required'
    } finally {
        if ($null -ne $password) {
            try { $password.Dispose() } catch { $metadata.passed = $false; $metadata.diagnostics.Add('Credential disposal failed') }
            $password = $null
        }
    }
    $metadata.classification = if ($metadata.passed) { 'passed' } else { 'failed' }
    try { Save-ClientReceipt } catch {
        $metadata.passed = $false
        [Console]::Error.WriteLine('Final public client receipt unavailable; validation failed')
    }
}
if (-not $metadata.passed) { [Console]::Error.WriteLine('Standard-user installer client failed; inspect client-observations.json and observations.json'); exit 1 }

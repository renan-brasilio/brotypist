# CI-only launcher shared by canonical xtask and authenticated artifact reuse.
# Never distribute this script or use it against a normal Windows profile.
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$InputFile,
    [Parameter(Mandatory)][string]$Work,
    [switch]$Bootstrap,
    [string]$HandoffHash
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$metadata = [ordered]@{
    schema = 1; tool = 'windows-standard-user-installer-client'; passed = $false
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
$base = $null; $key = $null; $createdKey = $false; $createdValue = $false
$nativeInitialized = $false
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
using System.Collections.Generic;
using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Security.Principal;
using System.Text;
public static class BroTypistStandardUserInstallerClient {
    public static Dictionary<string,object> ChildToken;
    public static bool LogonAttempted, ClientLaunched, ClientResumed, ClientExited, DeadlineExceeded, JobQuiescent;
    public static string ProfileDirectory;
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
    [DllImport("advapi32.dll", SetLastError=true)] static extern bool OpenProcessToken(IntPtr process,uint access,out IntPtr token);
    [DllImport("advapi32.dll", SetLastError=true)] static extern bool OpenThreadToken(IntPtr thread,uint access,bool openAsSelf,out IntPtr token);
    [DllImport("advapi32.dll", EntryPoint="GetTokenInformation", SetLastError=true)] static extern bool TokenUInt(IntPtr token,int kind,out uint value,int size,out int needed);
    [DllImport("advapi32.dll", EntryPoint="GetTokenInformation", SetLastError=true)] static extern bool TokenBuffer(IntPtr token,int kind,IntPtr value,int size,out int needed);
    [DllImport("advapi32.dll")] static extern IntPtr GetSidSubAuthorityCount(IntPtr sid);
    [DllImport("advapi32.dll")] static extern IntPtr GetSidSubAuthority(IntPtr sid,uint index);
    [DllImport("user32.dll", CharSet=CharSet.Unicode, ExactSpelling=true, SetLastError=true)]
    static extern IntPtr CreateWindowStationW(string name,uint flags,uint access,ref SecurityAttributes attributes);
    [DllImport("user32.dll", CharSet=CharSet.Unicode, ExactSpelling=true, SetLastError=true)]
    static extern IntPtr CreateDesktopW(string name,IntPtr device,IntPtr mode,uint flags,uint access,ref SecurityAttributes attributes);
    [DllImport("user32.dll", SetLastError=true)] static extern IntPtr GetProcessWindowStation();
    [DllImport("user32.dll", SetLastError=true)] static extern bool SetProcessWindowStation(IntPtr station);
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
    [DllImport("userenv.dll", CharSet=CharSet.Unicode, ExactSpelling=true, SetLastError=true)]
    static extern bool GetUserProfileDirectoryW(IntPtr token,StringBuilder path,ref uint size);
    static void Check(bool value) {
        if(!value) { LastNativeError=Marshal.GetLastWin32Error(); throw new Win32Exception(LastNativeError); }
    }
    static uint UInt(IntPtr token,int kind) {
        uint value; int needed; Check(TokenUInt(token,kind,out value,4,out needed)); return value;
    }
    static uint Integrity(IntPtr token) {
        int needed; TokenBuffer(token,25,IntPtr.Zero,0,out needed);
        if(needed<IntPtr.Size || needed>1024) throw new InvalidOperationException("Invalid integrity label length");
        IntPtr buffer=Marshal.AllocHGlobal(needed);
        try {
            Check(TokenBuffer(token,25,buffer,needed,out needed));
            IntPtr sid=Marshal.ReadIntPtr(buffer);
            byte count=Marshal.ReadByte(GetSidSubAuthorityCount(sid));
            if(count==0) throw new InvalidOperationException("Invalid integrity SID");
            return unchecked((uint)Marshal.ReadInt32(GetSidSubAuthority(sid,(uint)count-1)));
        } finally { Marshal.FreeHGlobal(buffer); }
    }
    static void RetainOwnership() {
        // Unknown live ownership is not a cleanup authorization. VM teardown
        // is the only fallback; never kill MSI services or close a live job.
        Console.Error.WriteLine("Native ownership retained until VM teardown; stage="+Stage+"; win32="+LastNativeError+"; deadline="+DeadlineExceeded);
        for(;;) System.Threading.Thread.Sleep(30000);
    }
    static void WaitOwned(IntPtr process,uint timeout) {
        uint wait=WaitForSingleObject(process,timeout);
        if(wait==258) DeadlineExceeded=true;
        if(wait!=0) {
            if(wait==0xffffffff) LastNativeError=Marshal.GetLastWin32Error();
            RetainOwnership();
        }
        ClientExited=true;
    }
    public static int Run(string executable,string command,string directory,string user,System.Security.SecureString password,string expectedSid,string supervisorSid) {
        IntPtr descriptor=IntPtr.Zero,secret=IntPtr.Zero,token=IntPtr.Zero,threadToken=IntPtr.Zero;
        ProcessInfo process=new ProcessInfo();
        if(command.Length>=1024) throw new InvalidOperationException("Logon command exceeds native limit");
        string stationName="btci-"+Guid.NewGuid().ToString("N");
        try {
            Stage="private_desktop";
            // A new object only; no WinSta0, shared desktop ACL, or desktop switch.
            string sddl="D:P(A;;GA;;;SY)(A;;GA;;;"+supervisorSid+")(A;;GA;;;"+expectedSid+")";
            Check(ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl,1,out descriptor,IntPtr.Zero));
            var attributes=new SecurityAttributes { size=Marshal.SizeOf<SecurityAttributes>(),descriptor=descriptor };
            IntPtr original=GetProcessWindowStation(); Check(original!=IntPtr.Zero);
            // GENERIC_ALL maps to the noninteractive station's supported rights.
            station=CreateWindowStationW(stationName,1,0x10000000,ref attributes); Check(station!=IntPtr.Zero);
            try {
                Check(SetProcessWindowStation(station));
                desktop=CreateDesktopW("Probe",IntPtr.Zero,IntPtr.Zero,0,0x10000000,ref attributes);
                Check(desktop!=IntPtr.Zero);
            } finally {
                if(!SetProcessWindowStation(original)) { LastNativeError=Marshal.GetLastWin32Error(); RetainOwnership(); }
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
            Stage="actual_child_token";
            // WindowsPrincipal membership checks duplicate a primary token.
            Check(OpenProcessToken(process.process,0x000A,out token));
            uint profileSize=0;
            GetUserProfileDirectoryW(token,null,ref profileSize);
            if(profileSize==0 || profileSize>32768) throw new InvalidOperationException("Invalid profile path length");
            var profile=new StringBuilder((int)profileSize);
            Check(GetUserProfileDirectoryW(token,profile,ref profileSize)); ProfileDirectory=profile.ToString();
            bool noImpersonation=!OpenThreadToken(process.thread,8,true,out threadToken);
            if(!noImpersonation || Marshal.GetLastWin32Error()!=1008) throw new InvalidOperationException("Child thread token is not absent");
            using(var identity=new WindowsIdentity(token)) {
                bool same=identity.User.Value==expectedSid,system=identity.IsSystem;
                bool administrator=new WindowsPrincipal(identity).IsInRole(WindowsBuiltInRole.Administrator);
                bool adminSid=false;
                foreach(var group in identity.Groups) if(group.Value=="S-1-5-32-544") adminSid=true;
                uint elevation=UInt(token,20),integrity=Integrity(token),type=UInt(token,8);
                ChildToken=new Dictionary<string,object> {
                    {"expected_user",same}, {"is_system",system}, {"administrator_role_enabled",administrator},
                    {"administrator_sid_present",adminSid}, {"token_elevation",elevation},
                    {"token_elevation_type",UInt(token,18)}, {"integrity_rid",integrity},
                    {"token_has_restrictions",UInt(token,21)}, {"token_type",type}, {"no_impersonation",noImpersonation}
                };
                if(!same || system || administrator || adminSid || elevation!=0 || integrity!=8192 || type!=1)
                    throw new InvalidOperationException("Actual child is not a genuine standard-user primary token");
            }
            Stage="job_assignment";
            Check(AssignProcessToJobObject(job,process.process));
            Stage="core_lifecycle";
            uint previousSuspendCount=ResumeThread(process.thread);
            ClientResumed=previousSuspendCount!=0xffffffff;
            Check(previousSuspendCount!=0xffffffff);
            if(previousSuspendCount!=1) throw new InvalidOperationException("Unexpected child suspend count");
            WaitOwned(process.process,900000);
            Stage="job_quiescence";
            JobAccounting accounting;
            Check(QueryInformationJobObject(job,1,out accounting,Marshal.SizeOf<JobAccounting>(),IntPtr.Zero));
            if(accounting.active!=0) RetainOwnership();
            JobQuiescent=true;
            uint code; Check(GetExitCodeProcess(process.process,out code));
            Stage="child_exited";
            return unchecked((int)code);
        } finally {
            if(ClientLaunched && !ClientExited) {
                if(ClientResumed) RetainOwnership();
                // Only the never-resumed child may be terminated safely.
                if(!TerminateProcess(process.process,1)) RetainOwnership();
                WaitOwned(process.process,30000); JobQuiescent=true;
            }
            if(threadToken!=IntPtr.Zero) CloseHandle(threadToken);
            if(token!=IntPtr.Zero) CloseHandle(token);
            if(process.thread!=IntPtr.Zero) CloseHandle(process.thread);
            if(process.process!=IntPtr.Zero) CloseHandle(process.process);
            if(secret!=IntPtr.Zero) Marshal.ZeroFreeGlobalAllocUnicode(secret);
            if(descriptor!=IntPtr.Zero) LocalFree(descriptor);
        }
    }
    public static void DeleteOwnedProfile(string sid) {
        Stage="profile_cleanup";
        Check(DeleteProfileW(sid,null,null));
    }
    public static void ReleaseObjects() {
        if(ClientLaunched && (!ClientExited || !JobQuiescent)) throw new InvalidOperationException("Live native objects retained");
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
function Save-ClientReceipt {
    Write-NewJson (Join-Path $Work 'client-observations.json') $metadata
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
function Hold-OwnedVm([string]$Reason) {
    $metadata.passed = $false
    $metadata.diagnostics.Add($Reason)
    try { Save-ClientReceipt } catch { [Console]::Error.WriteLine('Fail-closed client receipt unavailable') }
    [Console]::Error.WriteLine((@{
        stage = $metadata.stage; failure = $metadata.failure; cleanup_failure = $metadata.cleanup_failure
        account_creation_attempted = $accountCreationAttempted; account_created = $null -ne $account
        profile_absence_established = $profileWasAbsent; staging_created = $ownedRootCreated
        quiescence_verified = $metadata.quiescence_verified
        profile_cleanup_verified = $metadata.profile_cleanup_verified
        account_cleanup_verified = $metadata.account_cleanup_verified
        native_stage = if ($nativeInitialized) { [BroTypistStandardUserInstallerClient]::Stage } else { $null }
        native_error = if ($nativeInitialized) { [BroTypistStandardUserInstallerClient]::LastNativeError } else { $null }
    } | ConvertTo-Json -Depth 8 -Compress))
    [Console]::Error.WriteLine($Reason)
    while ($true) { [Threading.Thread]::Sleep(30000) }
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
    foreach ($private in @($ownedRoot, [BroTypistStandardUserInstallerClient]::ProfileDirectory, $accountName, $validationSid, $supervisorSid, $env:USERPROFILE)) {
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
        $metadata.actual_standard_user_token = [BroTypistStandardUserInstallerClient]::ChildToken
        $metadata.child_deadline_exceeded = [BroTypistStandardUserInstallerClient]::DeadlineExceeded
        $metadata.native_error_code = [BroTypistStandardUserInstallerClient]::LastNativeError
        $metadata.native_stage = [BroTypistStandardUserInstallerClient]::Stage
        $metadata.child_exit_confirmed = [BroTypistStandardUserInstallerClient]::ClientExited
        $metadata.genuine_standard_user = [BroTypistStandardUserInstallerClient]::ClientResumed
    }
}

if ($Bootstrap) {
    # This branch never creates accounts or touches host policy. Its immutable
    # handoff is authenticated by argv hash, not by inherited CI environment.
    try {
        Require ($IsWindows -and $HandoffHash -cmatch '^[0-9a-f]{64}$') 'Invalid bootstrap platform or handoff'
        $handoffPath = Join-Path $PSScriptRoot 'handoff.json'
        $handoffIdentity = File-Identity $handoffPath
        Require ($handoffIdentity.size -le 65536 -and $handoffIdentity.sha256 -ceq $HandoffHash) 'Bootstrap handoff identity differs'
        $handoff = Get-Content -LiteralPath $handoffPath -Raw | ConvertFrom-Json
        Require ($InputFile -ceq $handoff.input -and $Work -ceq $handoff.work) 'Bootstrap path handoff differs'
        $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
        try {
            Require ($identity.User.Value -ceq $handoff.expected_user_sid -and -not $identity.IsSystem -and -not [Security.Principal.WindowsPrincipal]::new($identity).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) 'Bootstrap account differs'
        } finally { $identity.Dispose() }
        foreach ($file in $handoff.files) {
            $actual = File-Identity $file.path
            Require ($actual.size -eq $file.size -and $actual.sha256 -ceq $file.sha256) 'Bootstrap staged file identity differs'
        }
        $env:CI = 'true'; $env:GITHUB_ACTIONS = 'true'
        $env:RUNNER_ENVIRONMENT = 'github-hosted'; $env:RUNNER_OS = 'Windows'
        $env:BROTYPIST_INSTALLER_SMOKE_DISPOSABLE = '1'; $env:RUNNER_TEMP = $handoff.root
        & $handoff.core -InputFile $InputFile -Work $Work -RequireStandardUser -ExpectedUserSid $handoff.expected_user_sid -SupervisorSid $handoff.supervisor_sid
        exit $LASTEXITCODE
    } catch {
        [Console]::Error.WriteLine('Standard-user bootstrap failed before core launch; private VM ownership retained by supervisor')
        exit 1
    }
}

try {
    Require ($IsWindows -and $env:CI -ceq 'true' -and $env:GITHUB_ACTIONS -ceq 'true' -and $env:RUNNER_ENVIRONMENT -ceq 'github-hosted' -and $env:RUNNER_OS -ceq 'Windows' -and $env:BROTYPIST_INSTALLER_SMOKE_DISPOSABLE -ceq '1') 'Disposable hosted Windows CI guard required'
    Require ([Runtime.InteropServices.RuntimeInformation]::OSArchitecture -eq [Runtime.InteropServices.Architecture]::X64) 'Native Windows x86_64 required'
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
    Require (-not (Test-Path -LiteralPath (Join-Path $Work 'client-observations.json'))) 'Existing client evidence refused'
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
    $metadata.passed = $true
} catch {
    $metadata.failure = Failure-Facts $_ $metadata.stage
    $metadata.diagnostics.Add(('Stage {0} failed ({1}); native observations and typed policy snapshots are authoritative' -f $metadata.stage, $_.Exception.GetType().Name))
    try { if ($null -ne $base) { $metadata.during = Policy-Snapshot $key } }
    catch { $metadata.diagnostics.Add('During-policy snapshot unavailable; never counted as passed') }
    $metadata.passed = $false
} finally {
    # Never race host-policy restoration with a live child, active/unknown MSI
    # service transaction, or owned UI/worker cleanup. A deadline does not free
    # the lease. Unprovable quiescence deliberately holds this runner until VM
    # teardown rather than returning/collecting an unfinished native outcome.
    $quiescent = -not $nativeInitialized -or -not [BroTypistStandardUserInstallerClient]::ClientLaunched
    $uninstallVerified = $quiescent
    if ($nativeInitialized -and [BroTypistStandardUserInstallerClient]::ClientLaunched -and [BroTypistStandardUserInstallerClient]::ClientExited -and [BroTypistStandardUserInstallerClient]::JobQuiescent) {
        if (-not [BroTypistStandardUserInstallerClient]::ClientResumed) {
            $quiescent = $true; $uninstallVerified = $true
        } else {
            try {
                $outcomePath = Join-Path $coreWork 'observations.json'
                $file = Assert-RegularPath $outcomePath
                Require ($file.Length -gt 0 -and $file.Length -le 262144) 'Native quiescence receipt missing/oversized'
                $outcome = Get-Content -LiteralPath $outcomePath -Raw | ConvertFrom-Json
                Assert-NativeQuiescence $outcome
                $quiescent = $true
                $uninstallVerified = $outcome.quiescence.uninstall_verified -is [bool] -and $outcome.quiescence.uninstall_verified
            } catch { $quiescent = $false }
        }
    }
    if (-not $quiescent) { Hold-OwnedVm 'Native quiescence unknown; retaining VM and policy lease until teardown, with no restoration or report promotion' }
    $metadata.quiescence_verified = $true
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
    $metadata.passed = $metadata.passed -and $metadata.restoration_verified
    if (-not $uninstallVerified) { Hold-OwnedVm 'Uninstall unverified; policy restoration attempted after quiescence, account/profile/tree retained until VM teardown' }
    try {
        $metadata.stage = 'report_promotion'
        if ($null -ne $outcome) {
            Promote-Report 'observations.json' $true
            Promote-Report 'tiny-worker/worker-smoke.json' $false
            Promote-Report 'portable-tiny-worker/worker-smoke.json' $false
            $metadata.passed = $metadata.passed -and $outcome.passed
        }
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
                if ([BroTypistStandardUserInstallerClient]::ClientLaunched) {
                    Require (-not [string]::IsNullOrWhiteSpace([BroTypistStandardUserInstallerClient]::ProfileDirectory) -and $profilePath -ieq [BroTypistStandardUserInstallerClient]::ProfileDirectory) 'Actual child profile and owned SID registration differ'
                }
                if ([BroTypistStandardUserInstallerClient]::LogonAttempted -and -not [BroTypistStandardUserInstallerClient]::ClientLaunched) {
                    Require ($null -ne $profilePath) 'Failed logon profile disposition unknown'
                }
                if ($null -ne $profilePath) {
                    Require ([IO.Path]::IsPathFullyQualified($profilePath) -and -not [string]::IsNullOrWhiteSpace($profilePath)) 'Owned profile path unavailable'
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
        $metadata.cleanup_failure = Failure-Facts $_ $metadata.stage
        $metadata.diagnostics.Add(('Cleanup stage {0} failed ({1}); no exception text or identifiers retained' -f $metadata.stage, $_.Exception.GetType().Name))
        Hold-OwnedVm 'Owned account/profile/tree cleanup unverified; retaining hosted VM until teardown'
    } finally { if ($null -ne $password) { $password.Dispose(); $password = $null } }
    $metadata.passed = $metadata.passed -and $metadata.genuine_standard_user -and $metadata.account_cleanup_verified -and $metadata.profile_cleanup_verified -and $metadata.private_tree_cleanup_verified
    $metadata.stage = 'complete'
    Save-ClientReceipt
}
if (-not $metadata.passed) { [Console]::Error.WriteLine('Standard-user installer client failed; inspect client-observations.json and observations.json'); exit 1 }

//! Disposable Windows CI installer evidence, deliberately distinct from a full release gate.
use crate::{common::*, package};
use anyhow::{Context, Result, ensure};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    collections::BTreeMap,
    fs::{self, File},
    io::Read,
    path::{Path, PathBuf},
    process::Command,
};

const RECEIPT: &str = "package-receipt-win32-x86_64.json";

pub fn run(bundle: &Path, assets: &Path, output: &Path) -> Result<()> {
    fs::create_dir_all(output)?;
    real_dir(output)?;
    let output = output.canonicalize()?;
    let report = output.join("windows-installer-smoke.json");
    ensure!(
        !report.try_exists()?,
        "refusing to replace installer evidence"
    );
    let mut receipt = json!({
        "schema": 1, "tool": "windows-core-installer-smoke", "runtime": "rust",
        "passed": false, "full_installer_matrix_passed": false,
        "scope": "fresh per-user MSI install, repair, relocated portable ZIP, installed CLI/doctor, visible native Quit, uninstall and synthetic-data continuity; not a complete production installer matrix",
        "signing": "unsigned", "publication": "held; Windows publication and OTA remain disabled",
        "interactive_typing_verified": false,
        "required_gates": {
            "upgrade": {"status":"not_exercised", "reason":"no genuine prior canonical package supplied"},
            "rollback": {"status":"not_exercised", "reason":"no genuine rollback-failure fixture supplied"}
        }
    });
    let result = execute(bundle, assets, &output, &mut receipt);
    receipt["passed"] = json!(result.is_ok());
    if result.is_err() {
        // Child diagnostics are fixed/bounded/redacted by the owned script. Never
        // serialize arbitrary subprocess output, settings or model text here.
        receipt["failure"] =
            json!("Windows installer smoke failed; inspect observations and redacted diagnostics");
    }
    json_new(&report, &receipt)?;
    result
}

fn execute(bundle: &Path, assets: &Path, output: &Path, receipt: &mut Value) -> Result<()> {
    receipt["stage"] = json!("disposable_native_ci_guard");
    ensure!(
        cfg!(target_os = "windows") && arch() == "x86_64",
        "native Windows x86_64 required"
    );
    ensure!(
        std::env::var("BROTYPIST_INSTALLER_SMOKE_DISPOSABLE").as_deref() == Ok("1")
            && std::env::var("CI").as_deref() == Ok("true")
            && std::env::var("GITHUB_ACTIONS").as_deref() == Ok("true")
            && std::env::var("RUNNER_ENVIRONMENT").as_deref() == Ok("github-hosted")
            && std::env::var("RUNNER_OS").as_deref() == Ok("Windows"),
        "explicit disposable GitHub-hosted Windows CI guard required"
    );
    let root = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../..")
        .canonicalize()?;
    let bundle = bundle.canonicalize()?;
    real_dir(assets)?;
    let assets = assets.canonicalize()?;
    ensure!(
        !output.starts_with(&bundle) && !output.starts_with(&assets),
        "evidence output must be separate from payload and artifacts"
    );
    receipt["stage"] = json!("canonical_clean_package_and_native_dependency_audit");
    let inspected = package::inspect(&root, &bundle, false)?;
    let package = verified_package(&assets)?;
    for field in [
        "application",
        "version",
        "source_commit",
        "architecture",
        "files",
        "native_provenance",
    ] {
        ensure!(
            package[field] == inspected[field],
            "canonical package and inspected bundle differ: {field}"
        );
    }
    ensure!(
        package["product_code"]
            == package::stable_guid(&format!("product/{}", package["version"].as_str().unwrap()))
            && package["upgrade_code"] == package::stable_guid("upgrade"),
        "Windows installer family identity differs"
    );
    receipt["version"] = package["version"].clone();
    receipt["source_commit"] = package["source_commit"].clone();
    receipt["package_sha256"] = json!(hash(&assets.join(RECEIPT))?);
    receipt["pe_native_dll_closure"] = json!({
        "passed":true, "method":"canonical bundle x86_64 PE and dumpbin dependency audit plus exact portable ZIP content; installed MSI payload is asserted separately by the native lifecycle probe",
        "development_bundle":false, "portable_zip_exact_content":true
    });
    receipt["stage"] = json!("optional_genuine_fixture_and_worker_preflight");
    let previous_dir = assets.join("upgrade-from");
    let previous = if previous_dir.try_exists()? {
        let previous = verified_package(&previous_dir)?;
        let current_version = validate_version(package["version"].as_str().unwrap())?;
        let prior_version = validate_version(
            previous["version"]
                .as_str()
                .context("prior version missing")?,
        )?;
        ensure!(
            prior_version < current_version
                && previous["upgrade_code"] == package["upgrade_code"]
                && previous["product_code"] != package["product_code"],
            "upgrade fixture must be a genuine older package in the same installer family"
        );
        Some(previous)
    } else {
        None
    };
    let worker = std::env::var_os("BROTYPIST_INSTALLER_SMOKE_WORKER_EXE").map(PathBuf::from);
    let model = std::env::var_os("BROTYPIST_INSTALLER_SMOKE_MODEL_PATH").map(PathBuf::from);
    ensure!(
        worker.is_some() == model.is_some(),
        "Tiny worker smoke executable and cached model must be supplied together"
    );
    let worker = worker.map(|path| path.canonicalize()).transpose()?;
    let model = model.map(|path| path.canonicalize()).transpose()?;
    if let (Some(worker), Some(model)) = (&worker, &model) {
        package::verify_binary(worker, "win32", "x86_64")?;
        regular(model)?;
    }
    // No rollback-failure MSI exists in the canonical package contract. Do not
    // synthesize failure properties or silently ignore a supplied fixture.
    ensure!(
        !assets.join("rollback-fixture.json").try_exists()?,
        "rollback fixture needs an explicitly reviewed native fixture contract"
    );
    let work = stage(output, "windows-installer-smoke")?;
    let input = work.join("input.json");
    json_new(
        &input,
        &json!({"package":package, "previous":previous,
        "worker":worker, "model":model}),
    )?;
    let script = root.join(".github/native/windows-installer-client.ps1");
    regular(&script)?;
    receipt["stage"] = json!("native_per_user_installer_lifecycle");
    let status = Command::new("pwsh.exe")
        .args(["-NoLogo", "-NoProfile", "-NonInteractive", "-File"])
        .arg(&*external_tool_path(&script))
        .arg("-InputFile")
        .arg(&*external_tool_path(&input))
        .arg("-Work")
        .arg(&*external_tool_path(&work))
        .arg("-ReportDirectory")
        .arg(&*external_tool_path(output))
        .stdin(std::process::Stdio::null())
        .status()
        .context("cannot invoke owned native installer probe")?;
    retain_native_evidence(&work, output, status.success(), receipt)?;
    receipt["stage"] = json!("completed_core_scope_only");
    Ok(())
}

fn retain_native_evidence(
    work: &Path,
    output: &Path,
    client_succeeded: bool,
    receipt: &mut Value,
) -> Result<()> {
    let client_path = output.join("client-observations.json");
    regular(&client_path)
        .context("shared standard-user client did not produce lifecycle evidence")?;
    ensure!(
        fs::metadata(&client_path)?.len() <= 256 * 1024,
        "standard-user client evidence exceeds limit"
    );
    let client: Value = serde_json::from_slice(&fs::read(client_path)?)?;
    receipt["standard_user_client"] = client.clone();
    // The public supervisor receipt is safe to retain even if the child never
    // exits. Do not inspect or promote private native output without quiescence.
    ensure!(
        client["quiescence_verified"] == true,
        "standard-user client quiescence is unverified; private evidence retained on disposable host"
    );
    let observed_path = work.join("observations.json");
    regular(&observed_path).context("native installer probe did not produce evidence")?;
    ensure!(
        fs::metadata(&observed_path)?.len() <= 256 * 1024,
        "native installer evidence exceeds limit"
    );
    let observed: Value = serde_json::from_slice(&fs::read(observed_path)?)?;
    if let Some(gates) = observed.get("required_gates") {
        receipt["required_gates"] = gates.clone();
    }
    receipt["observations"] = observed.clone();
    receipt["portable_runtime_passed"] = json!(observed["portable_runtime_passed"] == true);
    // Retain the canonical worker proof outside private staging even when the
    // independent MSI registration gate fails afterwards.
    for name in ["portable-tiny-worker", "tiny-worker"] {
        let worker_report = work.join(name).join("worker-smoke.json");
        if worker_report.try_exists()? {
            regular(&worker_report)?;
            ensure!(
                fs::metadata(&worker_report)?.len() <= 256 * 1024,
                "worker evidence exceeds limit"
            );
            let worker_observed: Value = serde_json::from_slice(&fs::read(&worker_report)?)?;
            json_new(&output.join(format!("{name}.json")), &worker_observed)?;
        }
    }
    ensure!(client_succeeded, "native installer client failed");
    validate_native_evidence(&client, &observed)?;
    Ok(())
}

fn validate_native_evidence(client: &Value, observed: &Value) -> Result<()> {
    for field in [
        "passed",
        "genuine_standard_user",
        "account_cleanup_verified",
        "profile_cleanup_verified",
        "restoration_verified",
        "quiescence_verified",
        "station_access_restoration_verified",
        "input_desktop_restoration_verified",
        "private_desktop_cleanup_verified",
    ] {
        ensure!(
            client[field] == true,
            "standard-user lifecycle evidence missing: {field}"
        );
    }
    ensure!(
        observed["passed"] == true
            && observed["observations"]
                .as_array()
                .context("native observations missing")?
                .iter()
                .filter(|row| {
                    row["check"] == "genuine_standard_user_verified_before_installer"
                        && row["passed"] == true
                })
                .count()
                == 1,
        "native installer assertions failed"
    );
    let mut desktops = observed["observations"]
        .as_array()
        .context("native observations missing")?
        .iter()
        .filter(|row| row["check"] == "owned_interactive_desktop_verified_before_installer");
    let desktop = desktops
        .next()
        .context("owned interactive desktop proof missing")?;
    ensure!(
        desktops.next().is_none(),
        "owned interactive desktop proof duplicated"
    );
    ensure!(
        desktop["passed"] == true,
        "owned interactive desktop verification failed"
    );
    for field in [
        "interactive_station",
        "expected_desktop_matches",
        "station_visible",
        "input_desktop_matches",
    ] {
        ensure!(
            desktop["evidence"][field] == true,
            "owned interactive desktop evidence missing: {field}"
        );
    }
    Ok(())
}

fn verified_package(directory: &Path) -> Result<Value> {
    real_dir(directory)?;
    let path = directory.join(RECEIPT);
    regular(&path)?;
    ensure!(
        fs::metadata(&path)?.len() < 8 * 1024 * 1024,
        "package receipt exceeds limit"
    );
    let mut value: Value = serde_json::from_slice(&fs::read(&path)?)?;
    ensure!(
        value["application"] == APP_ID
            && value["platform"] == "win32"
            && value["runtime"] == "rust"
            && value["architecture"] == "x86_64"
            && value["scope"] == "perUser"
            && value["signing"] == "unsigned"
            && value["release_eligible"] == true
            && value["native_provenance"]["release_eligible"] == true
            && value["native_provenance"]["source"]["dirty"] == false,
        "expected clean canonical unsigned per-user Rust package"
    );
    validate_version(value["version"].as_str().context("version missing")?)?;
    validate_commit(value["source_commit"].as_str().context("source missing")?)?;
    ensure!(
        value["product_code"]
            == package::stable_guid(&format!("product/{}", value["version"].as_str().unwrap()))
            && value["upgrade_code"] == package::stable_guid("upgrade"),
        "foreign MSI installer family"
    );
    for kind in ["msi", "zip"] {
        let recorded = PathBuf::from(
            value["artifacts"][kind]["path"]
                .as_str()
                .context("artifact path missing")?,
        );
        // Receipts remain usable after CI artifact download/relocation, but may
        // only resolve the exact named artifact alongside this receipt.
        let name = recorded.file_name().context("artifact basename missing")?;
        let artifact = directory.join(name);
        regular(&artifact)?;
        ensure!(
            value["artifacts"][kind]["sha256"] == hash(&artifact)?
                && value["artifacts"][kind]["size"] == fs::metadata(&artifact)?.len(),
            "artifact bytes differ from canonical receipt"
        );
        let canonical = artifact.canonicalize()?;
        value["artifacts"][kind]["path"] = json!(&*external_tool_path(&canonical));
    }
    let rows: Vec<Row> = serde_json::from_value(value["files"].clone())?;
    verify_zip(
        Path::new(value["artifacts"]["zip"]["path"].as_str().unwrap()),
        &rows,
    )?;
    Ok(value)
}

fn verify_zip(path: &Path, rows: &[Row]) -> Result<()> {
    let mut expected = BTreeMap::new();
    for row in rows {
        ensure!(
            safe_relative(Path::new(&row.path)) && !row.path.contains('\\'),
            "unsafe package inventory"
        );
        ensure!(
            expected
                .insert(format!("BroTypist/{}", row.path), row)
                .is_none(),
            "duplicate package file"
        );
    }
    let mut archive = zip::ZipArchive::new(File::open(path)?)?;
    ensure!(
        archive.len() == expected.len() && !expected.is_empty(),
        "portable ZIP file count differs"
    );
    let mut buffer = [0u8; 65536];
    for index in 0..archive.len() {
        let mut entry = archive.by_index(index)?;
        ensure!(
            !entry.is_dir()
                && entry.enclosed_name().is_some()
                && !entry.name().contains('\\')
                && safe_relative(Path::new(entry.name())),
            "unsafe portable ZIP path"
        );
        ensure!(
            entry
                .unix_mode()
                .is_none_or(|mode| mode & 0o170000 == 0 || mode & 0o170000 == 0o100000),
            "portable ZIP contains a link/special entry"
        );
        let row = expected
            .remove(entry.name())
            .context("unexpected or duplicate ZIP member")?;
        ensure!(
            entry.size() == row.size,
            "portable ZIP entry length differs"
        );
        let mut digest = Sha256::new();
        let mut read = 0u64;
        loop {
            let count = entry.read(&mut buffer)?;
            if count == 0 {
                break;
            }
            read += count as u64;
            ensure!(
                read <= row.size,
                "portable ZIP expands beyond declared length"
            );
            digest.update(&buffer[..count]);
        }
        ensure!(
            read == row.size && format!("{:x}", digest.finalize()) == row.sha256,
            "portable ZIP content differs"
        );
    }
    ensure!(expected.is_empty(), "portable ZIP omitted payload");
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn failed_public_client_receipt_survives_missing_or_unreadable_core() {
        let temp = tempfile::tempdir().unwrap();
        let output = temp.path().join("reports");
        let work = temp.path().join("private");
        fs::create_dir(&output).unwrap();
        fs::create_dir(&work).unwrap();
        let client_path = output.join("client-observations.json");
        let mut client = json!({
            "passed": false, "genuine_standard_user": false,
            "account_cleanup_verified": false, "profile_cleanup_verified": false,
            "restoration_verified": false, "quiescence_verified": false,
            "failure": "native_lifecycle_unresolved"
        });
        for quiescence in [json!(false), Value::Null, json!("true"), json!(true)] {
            client["quiescence_verified"] = quiescence;
            fs::write(&client_path, serde_json::to_vec(&client).unwrap()).unwrap();
            let mut receipt = json!({"passed": false});
            assert!(retain_native_evidence(&work, &output, false, &mut receipt).is_err());
            assert_eq!(receipt["standard_user_client"], client);
            assert_eq!(receipt["passed"], false);
            assert!(receipt.get("observations").is_none());
            assert!(work.is_dir());
        }
        // Even an existing private report must not be read or promoted when the
        // supervisor cannot establish process/MSI quiescence.
        fs::write(work.join("observations.json"), b"not safe to read").unwrap();
        client["quiescence_verified"] = json!(false);
        fs::write(&client_path, serde_json::to_vec(&client).unwrap()).unwrap();
        let mut receipt = json!({"passed": false});
        assert!(retain_native_evidence(&work, &output, false, &mut receipt).is_err());
        assert_eq!(receipt["standard_user_client"], client);
        assert_eq!(
            fs::read(work.join("observations.json")).unwrap(),
            b"not safe to read"
        );
        let mut artifacts = fs::read_dir(&output).unwrap();
        assert_eq!(
            artifacts.next().unwrap().unwrap().file_name(),
            "client-observations.json"
        );
        assert!(artifacts.next().is_none());
    }

    #[cfg(target_os = "windows")]
    #[test]
    fn unresolved_client_exits_and_retains_public_failure_without_cleanup() {
        let temp = tempfile::tempdir().unwrap();
        let reports = temp.path().join("reports");
        let private = temp.path().join("private");
        fs::create_dir(&reports).unwrap();
        fs::create_dir(&private).unwrap();
        for name in ["policy", "account", "profile", "tree"] {
            fs::write(private.join(name), b"must remain unresolved").unwrap();
        }
        let script = temp.path().join("unresolved-client-fixture.ps1");
        // Extract only the public failure/receipt seam. Never execute launcher
        // preflight, native initialization, account creation, policy or MSI APIs.
        fs::write(
            &script,
            r#"
param([string]$Source, [string]$ReportDirectory, [string]$Work)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$tokens = $null; $errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile($Source, [ref]$tokens, [ref]$errors)
if ($errors.Count -ne 0) { throw 'Client syntax errors' }
foreach ($name in @('Require', 'Write-NewJson', 'Observe-NativeDesktopLease', 'Save-ClientReceipt', 'Stop-OwnedVm')) {
    $definition = $ast.Find({ param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -ceq $name }, $true)
    if ($null -eq $definition) { throw "Missing failure guard: $name" }
    . ([scriptblock]::Create($definition.Extent.Text))
}
$reportDirectoryValidated = $true
$nativeInitialized = $false
$metadata = [ordered]@{
    schema = 1; passed = $false; classification = 'failed'; teardown_required = $false
    stage = 'fixture_unresolved'; failure = $null; cleanup_failure = $null
    genuine_standard_user = $false; account_cleanup_verified = $false
    profile_cleanup_verified = $false; restoration_verified = $false
    station_access_restoration_verified = $false; input_desktop_restoration_verified = $false
    private_desktop_cleanup_verified = $false; interactive_station_acl_modified = $false
    quiescence_verified = $false; uninstall_verified = $false
    diagnostics = [Collections.Generic.List[string]]::new()
}
try { Stop-OwnedVm 'Fixture: process/MSI quiescence is unresolved' } catch {
    [Console]::Error.WriteLine('Failure boundary threw instead of exiting with its public receipt')
    exit 2
}
# These are inert file stand-ins, not actual policy/accounts/profiles. A return
# from the fail-fast boundary would unsafely continue restoration and deletion.
[IO.File]::WriteAllText((Join-Path $Work 'policy'), 'restored unsafely')
foreach ($name in @('account', 'profile', 'tree')) { [IO.File]::Delete((Join-Path $Work $name)) }
exit 0
"#,
        )
        .unwrap();
        let native = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../.github/native");
        let mut child = Command::new("pwsh.exe")
            .args(["-NoLogo", "-NoProfile", "-NonInteractive", "-File"])
            .arg(&script)
            .arg(native.join("windows-installer-client.ps1"))
            .arg(&reports)
            .arg(&private)
            .stdin(std::process::Stdio::null())
            .spawn()
            .unwrap();
        let deadline = std::time::Instant::now() + std::time::Duration::from_secs(30);
        let status = loop {
            if let Some(status) = child.try_wait().unwrap() {
                break status;
            }
            if std::time::Instant::now() >= deadline {
                child.kill().unwrap();
                child.wait().unwrap();
                panic!("unresolved client retained an infinite-running process");
            }
            std::thread::sleep(std::time::Duration::from_millis(25));
        };
        assert_eq!(status.code(), Some(1));
        let mut receipt = json!({"passed": false});
        assert!(
            retain_native_evidence(&private, &reports, status.success(), &mut receipt).is_err()
        );
        let client = &receipt["standard_user_client"];
        assert_eq!(client["passed"], false);
        assert_eq!(client["classification"], "unresolved");
        assert_eq!(client["teardown_required"], true);
        for field in [
            "genuine_standard_user",
            "account_cleanup_verified",
            "profile_cleanup_verified",
            "restoration_verified",
            "quiescence_verified",
            "uninstall_verified",
            "station_access_restoration_verified",
            "input_desktop_restoration_verified",
            "private_desktop_cleanup_verified",
        ] {
            assert_eq!(client[field], false);
        }
        assert!(receipt.get("observations").is_none());
        assert!(!private.join("client-observations.json").exists());
        for name in ["policy", "account", "profile", "tree"] {
            assert_eq!(
                fs::read(private.join(name)).unwrap(),
                b"must remain unresolved"
            );
        }

        let reuse_script = temp.path().join("reuse-failure-fixture.ps1");
        fs::write(
            &reuse_script,
            r#"
param([string]$Source, [string]$Reports, [string]$Work)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$tokens = $null; $errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile($Source, [ref]$tokens, [ref]$errors)
if ($errors.Count -ne 0) { throw 'Reuse syntax errors' }
foreach ($name in @('Require', 'Write-NewBytes', 'Write-NewJson', 'Assert-StandardUserEvidence', 'Retain-StandardUserEvidence')) {
    $definition = $ast.Find({ param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -ceq $name }, $true)
    if ($null -eq $definition) { throw "Missing evidence guard: $name" }
    . ([scriptblock]::Create($definition.Extent.Text))
}
$metadata = [ordered]@{ passed = $false; native_exit_code = 1 }
$refused = $false
try { Retain-StandardUserEvidence $Reports $Work $metadata } catch { $refused = $true }
if (-not $refused -or $metadata.passed -or $metadata.standard_user_client.classification -cne 'unresolved' -or -not $metadata.standard_user_client.teardown_required) { throw 'Reuse lost unresolved failure evidence' }
if (Test-Path -LiteralPath (Join-Path $Reports 'windows-core-installer-smoke.json')) { throw 'Reuse promoted unverified native evidence' }
Write-NewJson (Join-Path $Reports 'reuse-failure.json') $metadata
$missingCoreReports = Join-Path $Reports 'missing-core'
[void][IO.Directory]::CreateDirectory($missingCoreReports)
$failedClient = Get-Content -LiteralPath (Join-Path $Reports 'client-observations.json') -Raw | ConvertFrom-Json
$failedClient.quiescence_verified = $true
$failedClient.classification = 'failed'
Write-NewJson (Join-Path $missingCoreReports 'client-observations.json') $failedClient
$metadata = [ordered]@{ passed = $false; native_exit_code = 1 }
$refused = $false
try { Retain-StandardUserEvidence $missingCoreReports $Work $metadata } catch { $refused = $true }
if (-not $refused -or $metadata.passed -or $metadata.standard_user_client.classification -cne 'failed' -or $metadata.standard_user_client.account_cleanup_verified) { throw 'Reuse discarded client metadata when core receipt was absent' }
"#,
        )
        .unwrap();
        let status = Command::new("pwsh.exe")
            .args(["-NoLogo", "-NoProfile", "-NonInteractive", "-File"])
            .arg(reuse_script)
            .arg(native.join("windows-installer-reuse.ps1"))
            .arg(&reports)
            .arg(&private)
            .status()
            .unwrap();
        assert!(status.success());
        let reuse: Value =
            serde_json::from_slice(&fs::read(reports.join("reuse-failure.json")).unwrap()).unwrap();
        assert_eq!(reuse["passed"], false);
        assert_eq!(reuse["native_exit_code"], 1);
        assert_eq!(
            reuse["standard_user_client"],
            receipt["standard_user_client"]
        );
        for name in ["policy", "account", "profile", "tree"] {
            assert_eq!(
                fs::read(private.join(name)).unwrap(),
                b"must remain unresolved"
            );
        }
    }

    #[test]
    fn native_evidence_requires_genuine_identity_and_complete_cleanup() {
        let client = json!({
            "passed": true, "genuine_standard_user": true,
            "account_cleanup_verified": true, "profile_cleanup_verified": true,
            "restoration_verified": true, "quiescence_verified": true,
            "station_access_restoration_verified": true,
            "input_desktop_restoration_verified": true, "private_desktop_cleanup_verified": true
        });
        let observed = json!({
            "passed": true,
            "observations": [{
                "check": "genuine_standard_user_verified_before_installer", "passed": true
            }, {
                "check": "owned_interactive_desktop_verified_before_installer", "passed": true,
                "evidence": {
                    "interactive_station": true, "expected_desktop_matches": true,
                    "station_visible": true, "input_desktop_matches": true
                }
            }]
        });
        assert!(validate_native_evidence(&client, &observed).is_ok());
        for field in client.as_object().unwrap().keys() {
            let mut missing = client.clone();
            missing.as_object_mut().unwrap().remove(field);
            assert!(validate_native_evidence(&missing, &observed).is_err());
            for invalid in [json!(false), json!("true"), json!(1), Value::Null] {
                let mut invalid_client = client.clone();
                invalid_client[field] = invalid;
                assert!(validate_native_evidence(&invalid_client, &observed).is_err());
            }
        }
        for field in [
            "passed",
            "interactive_station",
            "expected_desktop_matches",
            "station_visible",
            "input_desktop_matches",
        ] {
            for invalid in [None, Some(json!(false)), Some(json!("true"))] {
                let mut candidate = observed.clone();
                let row = &mut candidate["observations"][1];
                let target = if field == "passed" {
                    row
                } else {
                    &mut row["evidence"]
                };
                if let Some(value) = invalid {
                    target[field] = value;
                } else {
                    target.as_object_mut().unwrap().remove(field);
                }
                assert!(validate_native_evidence(&client, &candidate).is_err());
            }
        }
        let mut missing_desktop = observed.clone();
        missing_desktop["observations"]
            .as_array_mut()
            .unwrap()
            .pop();
        assert!(validate_native_evidence(&client, &missing_desktop).is_err());
        let mut duplicate_desktop = observed.clone();
        duplicate_desktop["observations"]
            .as_array_mut()
            .unwrap()
            .push(observed["observations"][1].clone());
        assert!(validate_native_evidence(&client, &duplicate_desktop).is_err());
        for invalid in [
            json!({"passed": false, "observations": observed["observations"]}),
            json!({"passed": true, "observations": []}),
            json!({"passed": true, "observations": [{"check": "genuine_standard_user_verified_before_installer", "passed": "true"}]}),
            json!({"passed": true, "observations": [observed["observations"][0], observed["observations"][0]]}),
        ] {
            assert!(validate_native_evidence(&client, &invalid).is_err());
        }
    }

    #[cfg(target_os = "windows")]
    #[test]
    fn private_station_acl_grant_restoration_and_foreign_mutation_are_verified() {
        let temp = tempfile::tempdir().unwrap();
        let script = temp.path().join("private-station-acl-fixture.ps1");
        // Real, newly created PRIVATE stations only: never open WinSta0, attach
        // a station, activate a desktop, create accounts, or invoke MSI.
        fs::write(
            &script,
            r#"
param([string]$Source)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$tokens = $null; $errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile($Source, [ref]$tokens, [ref]$errors)
if ($errors.Count -ne 0) { throw 'Client syntax errors' }
$definition = $ast.Find({ param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -ceq 'Initialize-InstallerNative' }, $true)
if ($null -eq $definition) { throw 'Native helper initialization missing' }
. ([scriptblock]::Create($definition.Extent.Text))
Initialize-InstallerNative
Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Security.AccessControl;
using System.Security.Principal;
public sealed class PrivateStationAclFixture : IDisposable {
    [StructLayout(LayoutKind.Sequential)] struct SecurityAttributes {
        public int length; public IntPtr descriptor; public int inherit;
    }
    [DllImport("user32.dll", CharSet=CharSet.Unicode, ExactSpelling=true, SetLastError=true)]
    static extern IntPtr CreateWindowStationW(string name,uint flags,uint access,ref SecurityAttributes attributes);
    [DllImport("user32.dll", SetLastError=true)] static extern bool CloseWindowStation(IntPtr handle);
    [DllImport("user32.dll", SetLastError=true)]
    static extern bool GetUserObjectSecurity(IntPtr handle,ref uint information,byte[] descriptor,uint length,out uint needed);
    [DllImport("user32.dll", SetLastError=true)]
    static extern bool SetUserObjectSecurity(IntPtr handle,ref uint information,byte[] descriptor);
    public const string Target = "S-1-5-21-987654321-876543219-765432198-999";
    public const string Foreign = "S-1-5-21-987654321-876543219-765432198-998";
    public IntPtr Handle { get; private set; }
    public PrivateStationAclFixture(bool preexisting) {
        using(var identity=WindowsIdentity.GetCurrent()) {
            string sid=identity.User.Value;
            var descriptor=new RawSecurityDescriptor("O:"+sid+"G:"+sid+
                "D:P(D;;0x2;;;"+Foreign+")(A;;0xf037f;;;"+sid+")(A;;0xf037f;;;SY)"+
                (preexisting ? "(A;;0x1;;;"+Target+")" : ""));
            byte[] bytes=new byte[descriptor.BinaryLength]; descriptor.GetBinaryForm(bytes,0);
            IntPtr memory=Marshal.AllocHGlobal(bytes.Length);
            try {
                Marshal.Copy(bytes,0,memory,bytes.Length);
                var attributes=new SecurityAttributes {
                    length=Marshal.SizeOf<SecurityAttributes>(),descriptor=memory,inherit=0
                };
                Handle=CreateWindowStationW("bt-acl-test-"+Guid.NewGuid().ToString("N"),1,0x60000,ref attributes);
                if(Handle==IntPtr.Zero) throw new Win32Exception(Marshal.GetLastWin32Error());
            } finally { Marshal.FreeHGlobal(memory); }
        }
    }
    public RawSecurityDescriptor Read() {
        uint information=4,needed;
        bool unexpected=GetUserObjectSecurity(Handle,ref information,null,0,out needed);
        int error=Marshal.GetLastWin32Error();
        if(unexpected || error!=122 || needed==0 || needed>65536) throw new InvalidOperationException("Invalid fixture security descriptor size");
        byte[] bytes=new byte[needed];
        if(!GetUserObjectSecurity(Handle,ref information,bytes,(uint)bytes.Length,out needed))
            throw new Win32Exception(Marshal.GetLastWin32Error());
        return new RawSecurityDescriptor(bytes,0);
    }
    static string AclBytes(RawAcl acl) {
        if(acl==null) throw new InvalidOperationException("Fixture requires nonnull DACL");
        byte[] bytes=new byte[acl.BinaryLength]; acl.GetBinaryForm(bytes,0);
        return Convert.ToBase64String(bytes);
    }
    public string Fingerprint() {
        var descriptor=Read();
        return ((int)descriptor.ControlFlags).ToString()+":"+AclBytes(descriptor.DiscretionaryAcl);
    }
    static string AceBytes(GenericAce ace) {
        byte[] bytes=new byte[ace.BinaryLength]; ace.GetBinaryForm(bytes,0);
        return Convert.ToBase64String(bytes);
    }
    public void AssertGrant(RawSecurityDescriptor before) {
        var after=Read();
        if(before.ControlFlags!=after.ControlFlags || after.DiscretionaryAcl.Count!=before.DiscretionaryAcl.Count+1)
            throw new InvalidOperationException("Grant changed DACL control or ACE count");
        int originalIndex=0,targets=0;
        foreach(GenericAce ace in after.DiscretionaryAcl) {
            var common=ace as CommonAce;
            if(common!=null && common.SecurityIdentifier.Value==Target) {
                targets++;
                if(common.AceQualifier!=AceQualifier.AccessAllowed || common.AceFlags!=AceFlags.None ||
                    common.IsCallback || common.AccessMask!=0x20337)
                    throw new InvalidOperationException("Grant is not the exact noninheriting station access ACE");
            } else {
                if(originalIndex>=before.DiscretionaryAcl.Count ||
                    AceBytes(ace)!=AceBytes(before.DiscretionaryAcl[originalIndex++]))
                    throw new InvalidOperationException("Grant changed an unrelated ACE");
            }
        }
        if(targets!=1 || originalIndex!=before.DiscretionaryAcl.Count)
            throw new InvalidOperationException("Grant lost original access or duplicated target");
    }
    public void MutateForeignDacl() {
        var descriptor=Read();
        descriptor.DiscretionaryAcl.InsertAce(descriptor.DiscretionaryAcl.Count,
            new CommonAce(AceFlags.None,AceQualifier.AccessAllowed,0x1,new SecurityIdentifier(Foreign),false,null));
        byte[] bytes=new byte[descriptor.BinaryLength]; descriptor.GetBinaryForm(bytes,0);
        uint information=4;
        if(!SetUserObjectSecurity(Handle,ref information,bytes))
            throw new Win32Exception(Marshal.GetLastWin32Error());
    }
    public void Dispose() {
        if(Handle!=IntPtr.Zero) {
            if(!CloseWindowStation(Handle)) throw new Win32Exception(Marshal.GetLastWin32Error());
            Handle=IntPtr.Zero;
        }
    }
}
'@
$fixture = [PrivateStationAclFixture]::new($false)
try {
    $original = $fixture.Read()
    $before = $fixture.Fingerprint()
    $lease = [BroTypistWindowStationAccess]::new($fixture.Handle, [PrivateStationAclFixture]::Target)
    if ($lease.Modified -or $fixture.Fingerprint() -cne $before) { throw 'Lease construction mutated station access before ownership was retained' }
    $lease.Apply()
    if (-not $lease.Modified -or $lease.Restored) { throw 'Grant receipt is false or prematurely restored' }
    $fixture.AssertGrant($original)
    $lease.Restore()
    if (-not $lease.Restored -or $fixture.Fingerprint() -cne $before) { throw 'Independent readback found restoration corruption' }
    # Do not repair or retry after foreign mutation. Closing the owned private
    # station destroys the fixture without touching any shared station.
    $lease = [BroTypistWindowStationAccess]::new($fixture.Handle, [PrivateStationAclFixture]::Target)
    $lease.Apply()
    $fixture.MutateForeignDacl()
    $foreign = $fixture.Fingerprint()
    $refused = $false
    try { $lease.Restore() } catch { $refused = $true }
    if (-not $refused -or $lease.Restored -or $fixture.Fingerprint() -cne $foreign) { throw 'Restoration overwrote foreign DACL mutation or claimed success' }
} finally { $fixture.Dispose() }
$fixture = [PrivateStationAclFixture]::new($true)
try {
    $before = $fixture.Fingerprint()
    $refused = $false
    try { $null = [BroTypistWindowStationAccess]::new($fixture.Handle, [PrivateStationAclFixture]::Target) } catch { $refused = $true }
    if (-not $refused -or $fixture.Fingerprint() -cne $before) { throw 'Preexisting target SID was accepted or modified' }
} finally { $fixture.Dispose() }
"#,
        )
        .unwrap();
        let source = Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../../.github/native/windows-installer-client.ps1");
        let status = Command::new("pwsh.exe")
            .args(["-NoLogo", "-NoProfile", "-NonInteractive", "-File"])
            .arg(script)
            .arg(source)
            .status()
            .unwrap();
        assert!(status.success());
    }

    #[cfg(target_os = "windows")]
    #[test]
    fn bounded_policy_and_real_write_rollback_evidence_fail_closed() {
        let temp = tempfile::tempdir().unwrap();
        let script = temp.path().join("bounded-installer-fixture.ps1");
        // Extract guards and exercise only owned temporary directories. Never
        // touch HKLM, create accounts, launch MSI, or claim native installer proof.
        fs::write(
            &script,
            r#"
param([string]$Native, [string]$Work)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
function Load-Guards([string]$File, [string[]]$Names) {
    $tokens = $null; $errors = $null
    $ast = [Management.Automation.Language.Parser]::ParseFile((Join-Path $Native $File), [ref]$tokens, [ref]$errors)
    if ($errors.Count -ne 0) { throw "Owned script syntax errors: $File" }
    foreach ($name in $Names) {
        $definition = $ast.Find({ param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -ceq $name }, $true)
        if ($null -eq $definition) { throw "Missing guard: $name" }
        # Dot-source into this caller's scope, not a discarded nested scope.
        $definition.Extent.Text
    }
}
foreach ($definition in @(Load-Guards 'windows-installer-client.ps1' @('Require', 'Assert-DisableMsiPolicy', 'Assert-NativeQuiescence', 'Initialize-InstallerNative', 'Private-DirectoryAcl', 'New-PrivateDirectory', 'Remove-OwnedTree', 'Remove-CreatedTree'))) { . ([scriptblock]::Create($definition)) }
# Compile owned P/Invoke declarations without calling any token/registry API.
Initialize-InstallerNative
$unowned = Join-Path $Work 'unowned-tree'
[void][IO.Directory]::CreateDirectory($unowned)
$sentinel = Join-Path $unowned 'preserve.txt'
[IO.File]::WriteAllText($sentinel, 'unowned data must survive')
Remove-CreatedTree (Join-Path $Work 'never-created') $false
$refused = $false
try { Remove-CreatedTree $unowned $false } catch { $refused = $true }
if (-not $refused -or [IO.File]::ReadAllText($sentinel) -cne 'unowned data must survive') { throw 'Unconfirmed tree ownership did not preserve its data' }
$created = Join-Path $Work 'created-tree'
if ($IsWindows) {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    try { $currentSid = $identity.User.Value } finally { $identity.Dispose() }
    New-PrivateDirectory $created (Private-DirectoryAcl $currentSid '' '')
    $acl = Get-Acl -LiteralPath $created
    if (-not $acl.AreAccessRulesProtected) { throw 'Private directory inherited unrelated access' }
    $allowed = @($currentSid, 'S-1-5-18') | Select-Object -Unique
    $rules = @($acl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier]))
    foreach ($rule in $rules) {
        if ($rule.IdentityReference.Value -notin $allowed -or $rule.AccessControlType -ne 'Allow') { throw 'Private directory granted an unrelated principal access' }
    }
    foreach ($sid in $allowed) {
        if (-not ($rules | Where-Object { $_.IdentityReference.Value -ceq $sid -and ($_.FileSystemRights -band [Security.AccessControl.FileSystemRights]::FullControl) -eq [Security.AccessControl.FileSystemRights]::FullControl })) { throw 'Private directory lost supervisor or SYSTEM access' }
    }
} else {
    [void][IO.Directory]::CreateDirectory($created)
    Write-Output 'Native Windows ACL creation is not exercised on this host.'
}
[void][IO.Directory]::CreateDirectory((Join-Path $created 'child'))
[IO.File]::WriteAllText((Join-Path $created 'child\owned.txt'), 'owned temporary data')
Remove-CreatedTree $created $true
if ((Test-Path -LiteralPath $created) -or [IO.File]::ReadAllText($sentinel) -cne 'unowned data must survive') { throw 'Owned-tree cleanup did not preserve its boundary' }
Assert-DisableMsiPolicy @{ present = $false; kind = $null; value = $null }
Assert-DisableMsiPolicy @{ present = $true; kind = 'DWord'; value = 0 }
$rejected = 0
foreach ($policy in @(
    @{ present = $true; kind = 'DWord'; value = 1 },
    @{ present = $true; kind = 'DWord'; value = 2 },
    @{ present = $true; kind = 'DWord'; value = -1 },
    @{ present = $true; kind = 'String'; value = '0' },
    @{ present = $true; kind = 'QWord'; value = 0 },
    @{ present = $true; kind = 'Binary'; value = @(0) }
)) {
    try { Assert-DisableMsiPolicy $policy } catch { $rejected++ }
}
if ($rejected -ne 6) { throw 'Explicit restrictive/type-invalid policy accepted' }
Assert-NativeQuiescence @{ quiescence = @{ installer_transactions_quiescent = $true; owned_processes_quiescent = $true } }
$rejected = 0
foreach ($outcome in @(
    @{ quiescence = @{ installer_transactions_quiescent = $false; owned_processes_quiescent = $true } },
    @{ quiescence = @{ installer_transactions_quiescent = $true; owned_processes_quiescent = $false } },
    @{ quiescence = @{ installer_transactions_quiescent = 'true'; owned_processes_quiescent = $true } },
    @{ quiescence = @{ installer_transactions_quiescent = $true; owned_processes_quiescent = 'true' } },
    @{ quiescence = @{} },
    @{}
)) {
    try { Assert-NativeQuiescence $outcome } catch { $rejected++ }
}
if ($rejected -ne 6) { throw 'Unknown/untyped quiescence accepted as permission to restore host policy' }
foreach ($definition in @(Load-Guards 'windows-installer-smoke.ps1' @('Observe', 'Assert-RollbackLog', 'Assert-StandardUserToken', 'Redact'))) { . ([scriptblock]::Create($definition)) }
$validToken = @{
    matches_expected_user = $true; is_system = $false
    administrator_role_enabled = $false; administrator_group_present = $false
    token_elevation = 0; integrity_rid = 8192; token_type = 1
    impersonation_level = 'None'; token_has_restrictions = 0
}
Assert-StandardUserToken $validToken
foreach ($invalid in @(
    @{ key = 'matches_expected_user'; value = $false },
    @{ key = 'is_system'; value = $true },
    @{ key = 'administrator_role_enabled'; value = $true },
    @{ key = 'administrator_group_present'; value = $true },
    @{ key = 'token_elevation'; value = 1 },
    @{ key = 'integrity_rid'; value = 12288 },
    @{ key = 'token_type'; value = 2 },
    @{ key = 'impersonation_level'; value = 'Impersonation' },
    @{ key = 'matches_expected_user'; value = 'true' }
)) {
    $token = $validToken.Clone(); $token[$invalid.key] = $invalid.value
    $refused = $false
    try { Assert-StandardUserToken $token } catch { $refused = $true }
    if (-not $refused) { throw 'Invalid identity or elevated/impersonated token accepted' }
}
$ExpectedUserSid = 'S-1-5-21-100-200-300-1001'
$ExpectedDesktopName = 'btci-00000000000000000000000000000000'
$SupervisorSid = 'S-1-5-21-100-200-300-1002'
$private = "$ExpectedUserSid $SupervisorSid $ExpectedDesktopName $env:USERNAME $env:USERPROFILE"
$redacted = Redact ($private.ToUpperInvariant())
foreach ($value in @($ExpectedUserSid, $SupervisorSid, $ExpectedDesktopName, $env:USERNAME, $env:USERPROFILE)) {
    if (-not [string]::IsNullOrWhiteSpace($value) -and $redacted.Contains($value, [StringComparison]::OrdinalIgnoreCase)) { throw 'Private validation identity leaked' }
}
foreach ($definition in @(Load-Guards 'windows-installer-reuse.ps1' @('Assert-StandardUserEvidence'))) { . ([scriptblock]::Create($definition)) }
$validClient = @{
    passed = $true; genuine_standard_user = $true
    account_cleanup_verified = $true; profile_cleanup_verified = $true
    restoration_verified = $true; quiescence_verified = $true
    station_access_restoration_verified = $true; input_desktop_restoration_verified = $true
    private_desktop_cleanup_verified = $true
}
$desktopProof = @{ check = 'owned_interactive_desktop_verified_before_installer'; passed = $true; evidence = @{
    interactive_station = $true; expected_desktop_matches = $true; station_visible = $true; input_desktop_matches = $true
} }
$validCore = @{ passed = $true; observations = @(@{ check = 'genuine_standard_user_verified_before_installer'; passed = $true }, $desktopProof) }
Assert-StandardUserEvidence $validClient $validCore
foreach ($field in @($validClient.Keys)) {
    foreach ($invalid in @($false, 'true', 1, $null)) {
        $candidate = $validClient.Clone(); $candidate[$field] = $invalid
        $refused = $false
        try { Assert-StandardUserEvidence $candidate $validCore } catch { $refused = $true }
        if (-not $refused) { throw 'Incomplete or untyped standard-user lifecycle accepted by reuse' }
    }
    $candidate = $validClient.Clone(); $candidate.Remove($field)
    $refused = $false
    try { Assert-StandardUserEvidence $candidate $validCore } catch { $refused = $true }
    if (-not $refused) { throw 'Missing standard-user lifecycle fact accepted by reuse' }
}
foreach ($field in @('passed', 'interactive_station', 'expected_desktop_matches', 'station_visible', 'input_desktop_matches')) {
    foreach ($invalid in @($false, 'true', $null)) {
        $row = $desktopProof.Clone(); $row.evidence = $desktopProof.evidence.Clone()
        $target = if ($field -ceq 'passed') { $row } else { $row.evidence }
        if ($null -eq $invalid) { $target.Remove($field) } else { $target[$field] = $invalid }
        $candidate = @{ passed = $true; observations = @($validCore.observations[0], $row) }
        $refused = $false
        try { Assert-StandardUserEvidence $validClient $candidate } catch { $refused = $true }
        if (-not $refused) { throw 'Missing, false or untyped desktop proof accepted by reuse' }
    }
}
foreach ($invalidCore in @(
    @{ passed = $false; observations = $validCore.observations },
    @{ passed = $true; observations = @() },
    @{ passed = $true; observations = @($validCore.observations[0]) },
    @{ passed = $true; observations = @($validCore.observations[0], $desktopProof, $desktopProof) },
    @{ passed = $true; observations = @(@{ check = 'genuine_standard_user_verified_before_installer'; passed = 'true' }) },
    @{ passed = $true; observations = @($validCore.observations[0], $validCore.observations[0]) }
)) {
    $refused = $false
    try { Assert-StandardUserEvidence $validClient $invalidCore } catch { $refused = $true }
    if (-not $refused) { throw 'Invalid core evidence accepted by reuse' }
}
$receipt = @{ observations = [Collections.Generic.List[object]]::new() }
$path = Join-Path $Work 'rollback.log'
$copy = 'MSI (s): Executing op: FileCopy(SourceName=synthetic,DestName=synthetic)'
$execute = 'Action ended 12:00:00: InstallExecute. Return value 1.'
$fault = 'Action start 12:00:01: BroTypist_CI_ONLY_RollbackFault.'
$message = 'BROTYPIST CI ONLY: intentional rollback after deferred MSI writes; NOT DISTRIBUTABLE'
$rollback = 'Rollback: synthetic'
[IO.File]::WriteAllText($path, (@($copy, $execute, $fault, $message, $rollback) -join "`n"), [Text.Encoding]::Unicode)
Assert-RollbackLog $path
if ($receipt.observations.Count -ne 1 -or -not $receipt.observations[0].passed) { throw 'Complete ordered fixture rejected' }
$rejected = 0
foreach ($fixture in @(
    @{ lines = @($execute, $fault, $message, $rollback) },
    @{ lines = @($copy, $fault, $message, $rollback) },
    @{ lines = @($copy, $execute, $message, $rollback) },
    @{ lines = @($copy, $execute, $fault, $rollback) },
    @{ lines = @($copy, $execute, $fault, $message) },
    @{ lines = @($fault, $copy, $execute, $message, $rollback) }
)) {
    [IO.File]::WriteAllText($path, ($fixture.lines -join "`n"), [Text.Encoding]::UTF8)
    try { Assert-RollbackLog $path } catch { $rejected++ }
}
if ($rejected -ne 6 -or $receipt.observations.Count -ne 1) { throw 'Pre-write or incomplete rollback evidence accepted' }
"#,
        )
        .unwrap();
        let native = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../.github/native");
        let status = Command::new("pwsh.exe")
            .args(["-NoLogo", "-NoProfile", "-NonInteractive", "-File"])
            .arg(script)
            .arg(native)
            .arg(temp.path())
            .status()
            .unwrap();
        assert!(status.success());
    }
    #[cfg(target_os = "windows")]
    #[test]
    fn historical_checkout_binding_requires_exact_initial_checkout_sha() {
        let temp = tempfile::tempdir().unwrap();
        let script = temp.path().join("checkout-fixture.ps1");
        fs::write(
            &script,
            r#"
param([string]$Source, [string]$Work)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$tokens = $null; $errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($Source, [ref]$tokens, [ref]$errors)
if ($errors.Count -ne 0) { throw 'Adapter syntax errors' }
foreach ($name in @('Require', 'Read-CheckoutIdentity')) {
    $definition = $ast.Find({ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -ceq $name }, $true)
    Invoke-Expression $definition.Extent.Text
}
$sha = '3a47450006b9ec09bffab38ca23465441c38e502'
$other = '0000000000000000000000000000000000000000'
$prefix = '2026-10-31T22:17:42.2047551Z '
$start = $prefix + '##[group]Run actions/checkout@v6'
$fetch = $prefix + '[command]"C:\Program Files\Git\bin\git.exe" -c protocol.version=2 fetch --depth=1 origin +' + $sha + ':refs/remotes/pull/1/merge'
$command = $prefix + '[command]"C:\Program Files\Git\bin\git.exe" log -1 --format=%H'
$finish = $prefix + '##[group]Run build'
$valid = @($start, $fetch, $command, ($prefix + $sha), $finish)
$path = Join-Path $Work 'checkout.log'
function Parse-Fixture([string[]]$Lines) {
    [IO.File]::WriteAllText($path, ($Lines -join "`n"))
    Read-CheckoutIdentity $path
}
$identity = Parse-Fixture $valid
if ($identity.commit -cne $sha -or $identity.pull_request -cne '1') { throw 'Historical exact checkout lost' }
# Later build output cannot replace the original checkout identity.
$identity = Parse-Fixture ($valid + @($command, ($prefix + $other)))
if ($identity.commit -cne $sha) { throw 'Later log output replaced checkout identity' }
$rejected = 0
foreach ($fixture in @(
    @{ lines = @($start, $fetch, $command, ($prefix + $other), $finish) },
    @{ lines = @($start, $fetch, $finish) },
    @{ lines = @($fetch, $command, ($prefix + $sha), $finish) },
    @{ lines = @($start, $fetch, $fetch, $command, ($prefix + $sha), $finish) },
    @{ lines = @($start, $fetch, $finish, $command, ($prefix + $sha)) }
)) {
    try { $null = Parse-Fixture $fixture.lines } catch { $rejected++ }
}
if ($rejected -ne 5) { throw 'Checkout missing/mismatched/duplicate/out-of-step evidence accepted' }
"#,
        )
        .unwrap();
        let source = Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../../.github/native/windows-installer-reuse.ps1");
        let status = Command::new("pwsh.exe")
            .args(["-NoLogo", "-NoProfile", "-NonInteractive", "-File"])
            .arg(script)
            .arg(source)
            .arg(temp.path())
            .status()
            .unwrap();
        assert!(status.success());
    }
    #[cfg(target_os = "windows")]
    #[test]
    fn routing_evidence_retains_owned_hives_without_raw_log_content() {
        let temp = tempfile::tempdir().unwrap();
        let script = temp.path().join("scope-fixture.ps1");
        // Load only the parser's function AST: never execute the installer probe.
        // Both native roots must survive; other products, properties and paths must not.
        fs::write(
            &script,
            r#"
param([string]$Source, [string]$Work)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$tokens = $null; $errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile($Source, [ref]$tokens, [ref]$errors)
if ($errors.Count -ne 0) { throw 'Probe syntax errors' }
$definition = $ast.Find({ param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -ceq 'Observe-MsiRouting' }, $true)
Invoke-Expression $definition.Extent.Text
$products = @('{ED0E8FF5-4909-5BF5-A22D-D8F924EB7C88}')
$receipt = @{ observations = [Collections.Generic.List[object]]::new() }
$path = Join-Path $Work 'fixture.log'
$fixture = @'
Action start 22:34:44: RegisterProduct.
MSI (s) (04:E0): Executing op: RegOpenKey(Root=-2147483646,Key=Software\Microsoft\Windows\CurrentVersion\Uninstall\{ED0E8FF5-4909-5BF5-A22D-D8F924EB7C88},BinaryType=1,)
MSI (s) (04:E0): Executing op: RegOpenKey(Root=-2147483647,Key=Software\Microsoft\Windows\CurrentVersion\Uninstall\{ED0E8FF5-4909-5BF5-A22D-D8F924EB7C88},BinaryType=1,)
MSI (s) (04:E0): Executing op: RegOpenKey(Root=-2147483646,Key=Software\Microsoft\Windows\CurrentVersion\Uninstall\{00000000-0000-0000-0000-000000000000},BinaryType=1,)
Property(S): ALLUSERS = 1
Property(S): MSIINSTALLPERUSER = 1
Property(S): Privileged = 1
Property(S): AdminUser = 1
Property(S): MsiRunningElevated = 1
MSI (s) (04:E0): Machine policy value 'AlwaysInstallElevated' is 0
Property(S): PRIVATE = never-retain-this-value
MSI (s): C:\Users\never-retain-this-path\payload
'@
[IO.File]::WriteAllText($path, $fixture, [Text.Encoding]::Unicode)
Observe-MsiRouting 'fixture' $path
$facts = @($receipt.observations[0].evidence.facts)
if ($facts.Count -ne 9) { throw 'Owned routing or allowlisted engine facts missing or unrelated content retained' }
if (@($facts | Where-Object { $_.event -eq 'owned_arp_reg_open' }).Count -ne 2) { throw 'Native hive distinction lost' }
if (@($facts | Where-Object { $_.event -eq 'engine_privilege_property' }).Count -ne 3) { throw 'Actual engine privilege diagnostics lost' }
if (@($facts | Where-Object { $_.event -eq 'engine_installer_policy' -and $_.scope -ceq 'Machine' -and $_.policy -ceq 'AlwaysInstallElevated' -and $_.value -ceq '0' }).Count -ne 1) { throw 'Actual engine policy diagnostic lost' }
$json = $receipt | ConvertTo-Json -Depth 16
if ($json.Contains('never-retain') -or $json.Contains('00000000-0000')) { throw 'Private or unrelated log content retained' }
[IO.File]::WriteAllText($path, ('Property(S): ALLUSERS = 1' + "`n") * 300)
Observe-MsiRouting 'bounded' $path
if (-not $receipt.observations[1].evidence.truncated -or @($receipt.observations[1].evidence.facts).Count -ne 256) { throw 'Routing fact limit failed' }
if ($null -ne $receipt.observations[1].passed) { throw 'Diagnostic observation was promoted to passing gate' }
"#,
        )
        .unwrap();
        let source = Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../../.github/native/windows-installer-smoke.ps1");
        let status = Command::new("pwsh.exe")
            .args(["-NoLogo", "-NoProfile", "-NonInteractive", "-File"])
            .arg(script)
            .arg(source)
            .arg(temp.path())
            .status()
            .unwrap();
        assert!(status.success());
    }
    #[test]
    fn portable_zip_rejects_extra_and_traversal_members() {
        let temp = tempfile::tempdir().unwrap();
        let zip_path = temp.path().join("payload.zip");
        let row = Row {
            path: "asset".into(),
            size: 3,
            sha256: format!("{:x}", Sha256::digest(b"abc")),
        };
        for name in [
            "BroTypist/asset",
            "BroTypist/../asset",
            "BroTypist/asset:stream",
            "elsewhere/asset",
        ] {
            let mut archive = zip::ZipWriter::new(File::create(&zip_path).unwrap());
            archive
                .start_file(name, zip::write::SimpleFileOptions::default())
                .unwrap();
            std::io::Write::write_all(&mut archive, b"abc").unwrap();
            archive.finish().unwrap();
            assert_eq!(
                verify_zip(&zip_path, std::slice::from_ref(&row)).is_ok(),
                name == "BroTypist/asset"
            );
        }
    }
}

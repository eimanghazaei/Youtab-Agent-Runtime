//! Filesystem paths + logging setup.
//!
//! Mirrors `youtab_constants.get_youtab_home()` from the Python CLI:
//!   Windows: %LOCALAPPDATA%\youtab
//!   macOS:   ~/.youtab-agent-runtime
//!   Linux:   ~/.youtab-agent-runtime  (override via $YOUTAB_AGENT_HOME)
//!
//! NOTE (macOS): Python's get_youtab_home(), scripts/install.sh, and the
//! Electron desktop's resolveYoutabHome() ALL use ~/.youtab-agent-runtime on macOS — there
//! is no ~/Library/Application Support branch anywhere else. An earlier
//! version of this file used Application Support, which drifted from every
//! other component: the installer wrote the install to one dir and the
//! desktop looked for it in another, so first launch never found the backend.
//!
//! IMPORTANT: this must match exactly. Drift here means install.ps1
//! writes to one place and the installer reads from another, breaking
//! the bootstrap-complete check.

use std::path::{Path, PathBuf};
#[cfg(target_os = "macos")]
use std::process::Command;
use tracing_appender::non_blocking::WorkerGuard;

/// Returns the canonical Youtab home directory, respecting $YOUTAB_AGENT_HOME if set.
pub fn youtab_home() -> PathBuf {
    if let Ok(override_path) = std::env::var("YOUTAB_AGENT_HOME") {
        if !override_path.trim().is_empty() {
            return PathBuf::from(override_path);
        }
    }

    #[cfg(target_os = "windows")]
    {
        // %LOCALAPPDATA%\youtab — matches scripts/install.ps1's $YoutabHome.
        if let Some(local_app_data) = dirs::data_local_dir() {
            return local_app_data.join("youtab");
        }
    }

    // macOS + Linux + fallback: ~/.youtab-agent-runtime (matches Python get_youtab_home(),
    // install.sh, and the Electron desktop's resolveYoutabHome()).
    if let Some(home) = dirs::home_dir() {
        return home.join(".youtab-agent-runtime");
    }

    // Last resort — current dir, almost certainly wrong but at least
    // doesn't panic.
    PathBuf::from(".youtab-agent-runtime")
}

pub fn log_dir() -> PathBuf {
    youtab_home().join("logs")
}

pub fn log_path() -> PathBuf {
    log_dir().join("bootstrap-installer.log")
}

pub fn bootstrap_cache_dir() -> PathBuf {
    youtab_home().join("bootstrap-cache")
}

/// Stable location the installer copies itself to after a successful install.
/// The desktop app re-invokes this with `--update`, and the start-menu /
/// desktop shortcuts can point users back to it. Lives directly under
/// YOUTAB_AGENT_HOME so it survives repo checkout deletion (unlike anything under
/// youtab-agent-runtime/).
///
/// On Windows this is `%LOCALAPPDATA%\youtab\youtab-setup.exe`; on other
/// platforms the extension differs but the directory is the same.
pub fn installer_dest() -> PathBuf {
    installer_dest_in(&youtab_home())
}

pub(crate) fn installer_dest_in(home: &Path) -> PathBuf {
    let name = if cfg!(target_os = "windows") {
        "youtab-setup.exe"
    } else {
        "youtab-setup"
    };
    home.join(name)
}

/// Fingerprint a regular, non-empty Setup. The customer transaction compares
/// against the currently running executable, not an untrusted downloaded file.
pub(crate) fn installer_sha256(path: &Path) -> std::io::Result<String> {
    use std::io::Read;
    use sha2::{Digest, Sha256};
    let metadata = std::fs::symlink_metadata(path)?;
    if !metadata.is_file() || metadata.len() == 0 {
        return Err(std::io::Error::new(std::io::ErrorKind::InvalidData, "Setup must be a non-empty regular file"));
    }
    let mut input = std::fs::File::open(path)?;
    let mut hash = Sha256::new();
    let mut buffer = [0u8; 64 * 1024];
    loop {
        let count = input.read(&mut buffer)?;
        if count == 0 { break; }
        hash.update(&buffer[..count]);
    }
    Ok(format!("{:x}", hash.finalize()))
}

pub(crate) fn verify_installer(path: &Path, expected_sha256: &str) -> std::io::Result<()> {
    if installer_sha256(path)? != expected_sha256 {
        return Err(std::io::Error::new(std::io::ErrorKind::InvalidData, "Setup copy verification failed"));
    }
    Ok(())
}

/// Write a private sibling staging file. Never truncate the installed Setup.
/// Partial staging files are owned/recovered by the existing install journal.
pub(crate) fn stage_installer(source: &Path, staged: &Path, expected_sha256: &str) -> std::io::Result<()> {
    let mut input = std::fs::File::open(source)?;
    let mut output = std::fs::OpenOptions::new().write(true).create_new(true).open(staged)?;
    std::io::copy(&mut input, &mut output)?;
    output.sync_all()?;
    drop(output);
    verify_installer(staged, expected_sha256)
}

/// Marker the updater writes for the duration of an in-app update and removes
/// when it finishes (see update.rs `UpdateMarkerGuard`). A freshly-launched
/// desktop checks this before spawning its own local backend: spawning one
/// mid-update re-locks the venv shim and triggers `force_kill_other_youtab`,
/// which then kills that legitimate backend in a respawn loop (#50238).
///
/// Lives directly under YOUTAB_AGENT_HOME (same rationale as `installer_dest`) so the
/// Electron desktop — which resolves YOUTAB_AGENT_HOME identically and pins it into
/// the updater's env — agrees on the exact path.
pub fn update_in_progress_marker() -> PathBuf {
    youtab_home().join(".youtab-agent-runtime-update-in-progress")
}

/// Copy the currently-running installer binary to `installer_dest()` so it's
/// available for future `--update` runs and shortcut launches.
///
/// No-ops (returns Ok) when the running exe is ALREADY the destination — which
/// is exactly the case during an `--update` run (the desktop launched us FROM
/// that path), where copying onto ourselves would be a Windows sharing
/// violation. Best-effort: a failure here must not fail the install, so the
/// caller logs and continues.
pub fn copy_self_to_youtab_home() -> std::io::Result<()> {
    let src = std::env::current_exe()?;
    let dest = installer_dest();

    // Skip if we're already running from the destination (update re-invocation
    // or a prior copy). canonicalize both so symlinks / 8.3 short paths / case
    // differences don't trick us into a self-copy.
    let same = match (src.canonicalize(), dest.canonicalize()) {
        (Ok(a), Ok(b)) => a == b,
        _ => src == dest,
    };
    if same {
        tracing::info!(?dest, "installer already at destination; skipping self-copy");
        return Ok(());
    }

    if let Some(parent) = dest.parent() {
        std::fs::create_dir_all(parent)?;
    }
    std::fs::copy(&src, &dest)?;
    repair_macos_installer_helper(&dest);
    tracing::info!(?src, ?dest, "copied installer to YOUTAB_AGENT_HOME");
    Ok(())
}

#[cfg(target_os = "macos")]
fn repair_macos_installer_helper(path: &Path) {
    // The staged helper may inherit quarantine from the downloaded installer.
    // Desktop later launches this exact file for in-app updates, so make it
    // executable before the update handoff reaches LaunchServices/Gatekeeper.
    let _ = Command::new("/usr/bin/xattr")
        .args(["-cr"])
        .arg(path)
        .status();

    let verify = Command::new("/usr/bin/codesign")
        .arg("--verify")
        .arg(path)
        .status();

    if !matches!(verify, Ok(status) if status.success()) {
        let _ = Command::new("/usr/bin/codesign")
            .args(["--force", "--sign", "-"])
            .arg(path)
            .status();
    }
}

#[cfg(not(target_os = "macos"))]
fn repair_macos_installer_helper(_path: &Path) {}

/// Where the bootstrap-complete marker lives (existence-only for the Rust
/// installer fast path; JSON schema-checked by the Electron app). Per main.ts:
///   const BOOTSTRAP_COMPLETE_MARKER = path.join(ACTIVE_YOUTAB_AGENT_ROOT, '.youtab-agent-runtime-bootstrap-complete')
/// We don't always know ACTIVE_YOUTAB_AGENT_ROOT until install.ps1 reports it, so
/// this is a probe helper, not a definitive path.
pub fn likely_bootstrap_marker(install_root: &Path) -> PathBuf {
    install_root.join(".youtab-agent-runtime-bootstrap-complete")
}

/// Initializes tracing to bootstrap-installer.log under YOUTAB_AGENT_HOME/logs/.
/// Returns a guard that flushes the appender on drop — keep it alive for
/// the lifetime of the process.
pub fn init_logging() -> Option<WorkerGuard> {
    let dir = log_dir();
    if let Err(err) = std::fs::create_dir_all(&dir) {
        // No log dir → log to stderr only. Don't panic; the installer
        // should still be usable on an exotic filesystem.
        eprintln!("[youtab-setup] could not create log dir {dir:?}: {err}");
        return None;
    }

    let file_appender = tracing_appender::rolling::never(&dir, "bootstrap-installer.log");
    let (non_blocking, guard) = tracing_appender::non_blocking(file_appender);

    let env_filter = tracing_subscriber::EnvFilter::try_from_env("YOUTAB_AGENT_BOOTSTRAP_LOG")
        .unwrap_or_else(|_| tracing_subscriber::EnvFilter::new("info"));

    tracing_subscriber::fmt()
        .with_env_filter(env_filter)
        .with_writer(non_blocking)
        .with_ansi(false)
        .with_target(true)
        .init();

    Some(guard)
}

// ---------------------------------------------------------------------------
// Tauri commands
// ---------------------------------------------------------------------------

#[tauri::command]
pub fn get_log_path() -> String {
    log_path().to_string_lossy().into_owned()
}

#[tauri::command]
pub fn get_youtab_home() -> String {
    youtab_home().to_string_lossy().into_owned()
}

#[tauri::command]
pub fn open_log_dir(app: tauri::AppHandle) -> Result<(), String> {
    use tauri_plugin_opener::OpenerExt;
    let path = log_dir();
    app.opener()
        .open_path(path.to_string_lossy(), None::<&str>)
        .map_err(|e| e.to_string())
}

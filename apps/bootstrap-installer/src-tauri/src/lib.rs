//! Youtab Setup — Tauri entrypoint.
//!
//! Spawns a single window pointed at the React frontend (apps/bootstrap-installer/src/).
//! All install-time work lives in `bootstrap.rs` and is invoked through the Tauri
//! commands registered at the bottom of `run()`.
//!
//! The Windows-subsystem strip lives on the binary crate (src/main.rs), not
//! here — a crate-level attribute on a lib doesn't propagate to the linker
//! flags of the executable that consumes it.

mod bootstrap;
mod events;
mod install_script;
mod powershell;
mod paths;
mod update;

use std::sync::Arc;
use tokio::sync::Mutex;

/// How the installer was invoked. Resolved once from the process args in
/// `run()` and exposed to the frontend via `get_mode` so it can route to the
/// install flow (first-run onboarding) or the update flow (driven by the
/// desktop app handing off via `Youtab-Setup.exe --update`).
///
/// Bare launch (double-click, first-run) => Install.
/// `--update` (spawned by the desktop's "Update" button) => Update.
#[derive(Debug, Clone, Copy, PartialEq, Eq, serde::Serialize)]
#[serde(rename_all = "lowercase")]
pub enum AppMode {
    Install,
    Update,
}

impl AppMode {
    /// Resolve the mode from an argument iterator. Anything containing the
    /// `--update` flag selects Update; otherwise Install. Kept arg-iterator
    /// generic (not reading `std::env` directly) so it's unit-testable.
    pub fn from_args<I, S>(args: I) -> Self
    where
        I: IntoIterator<Item = S>,
        S: AsRef<str>,
    {
        for a in args {
            if a.as_ref() == "--update" {
                return AppMode::Update;
            }
        }
        AppMode::Install
    }
}

/// Returns true when the args request a forced installer UI (repair/reinstall)
/// via `--reinstall` or `--repair`, which overrides the macOS launcher
/// fast-path so a broken install can be repaired. Arg-iterator generic so it's
/// unit-testable, mirroring `AppMode::from_args`. Independent of mode selection:
/// these flags never flip Install<->Update.
pub fn force_setup_from_args<I, S>(args: I) -> bool
where
    I: IntoIterator<Item = S>,
    S: AsRef<str>,
{
    args.into_iter()
        .any(|a| a.as_ref() == "--reinstall" || a.as_ref() == "--repair" || a.as_ref() == "--migrate-legacy")
}

/// Process-wide install state, shared across Tauri commands.
///
/// The bootstrap is a one-shot, single-tenant process — we only need one
/// of these per window. `Arc<Mutex<...>>` lets command handlers grab it
/// without lifetime gymnastics.
pub struct AppState {
    pub bootstrap: Mutex<Option<bootstrap::BootstrapHandle>>,
    /// How this process was launched (install vs update). Immutable for the
    /// lifetime of the process; read by the `get_mode` command.
    pub mode: AppMode,
}

impl AppState {
    fn new(mode: AppMode) -> Self {
        Self {
            bootstrap: Mutex::new(None),
            mode,
        }
    }
}

/// Frontend → Rust: which flow should the UI render?
#[tauri::command]
fn get_mode(state: tauri::State<'_, Arc<AppState>>) -> AppMode {
    state.mode
}

#[tauri::command]
fn get_release_channel() -> Result<String, String> {
    update::release_channel_for_install(&paths::youtab_home().join("youtab-agent-runtime")).map_err(|error| error.to_string())
}

#[tauri::command]
fn get_installation_kind() -> Result<String, String> {
    let root = paths::youtab_home().join("youtab-agent-runtime");
    installation_kind(&root, option_env!("RELEASE_BASE_URL").is_some() && cfg!(target_os = "windows"))
}

fn installation_kind(root: &std::path::Path, legacy_allowed: bool) -> Result<String, String> {
    if root.is_symlink() {return Err("Installation needs review before replacement".to_owned());}
    if !root.exists() {return Ok("fresh".to_owned());}
    let marker_path = root.join(".youtab-agent-runtime-bootstrap-complete");
    if marker_path.is_symlink() {return Err("Installation needs review before replacement".to_owned());}
    if std::fs::metadata(&marker_path).map_err(|_| "Installation needs review before replacement")?.len() > 65536 {
        return Err("Installation metadata is too large".to_owned());
    }
    let bytes = std::fs::read(marker_path).map_err(|_| "Installation needs review before replacement".to_owned())?;
    let marker: serde_json::Value = serde_json::from_slice(&bytes).map_err(|_| "Installation metadata is unreadable".to_owned())?;
    if marker.get("releaseSequence").is_some_and(|v| !v.is_null()) {
        let valid_hex = |key: &str, length: usize| marker.get(key).and_then(serde_json::Value::as_str)
            .is_some_and(|value| value.len() == length && value.bytes().all(|b| b.is_ascii_hexdigit() && !b.is_ascii_uppercase()));
        if marker.get("releaseSequence").and_then(serde_json::Value::as_u64).unwrap_or(0) == 0
            || !valid_hex("pinnedCommit", 40) || !valid_hex("artifactSha256", 64)
            || marker.get("releaseBaseUrl").and_then(serde_json::Value::as_str).is_none() {
            return Err("Installation metadata needs review".to_owned());
        }
        return Ok("artifact".to_owned());
    }
    if marker.get("releaseBaseUrl").is_some_and(|v| !v.is_null()) || marker.get("artifactSha256").is_some_and(|v| !v.is_null()) {
        return Err("Installation metadata needs review".to_owned());
    }
    if legacy_allowed && !root.join(".git").is_symlink() && root.join(".git").is_dir() {
        return Ok("legacy".to_owned());
    }
    Err("This existing installation cannot be replaced automatically".to_owned())
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    // Tracing → bootstrap-installer.log under YOUTAB_AGENT_HOME/logs/ so install
    // failures leave a trail for support. Console output also goes here in
    // debug builds.
    let _guard = paths::init_logging();

    let mode = AppMode::from_args(std::env::args().skip(1));
    // Escape hatch: `--reinstall`/`--repair` forces the installer UI even when
    // Youtab is already installed, so users can re-run setup to repair a broken
    // install instead of the launcher fast path silently relaunching the app.
    let force_setup = force_setup_from_args(std::env::args().skip(1));
    tracing::info!(?mode, force_setup, "Youtab installer starting");

    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_process::init())
        .plugin(tauri_plugin_shell::init())
        .manage(Arc::new(AppState::new(mode)))
        .setup(move |app| {
            use tauri::Manager;
            // Launcher fast path (macOS only): a bare ("Install") launch when
            // Youtab is already installed should NOT show the installer or
            // rebuild — it should just open the app, so the /Applications
            // "Youtab" doubles as a normal launcher (first run installs, every
            // later run launches instantly). The window is kept hidden until
            // here via `"visible": false` so this path never flashes a window.
            //
            // Gated to macOS deliberately: on Windows/Linux the installer keeps
            // its existing behavior (Windows users relaunch via the Start
            // Menu/Desktop "Youtab" shortcuts that install.ps1 creates, and a
            // reliable detached relaunch there needs the DETACHED_PROCESS +
            // startup-grace handling used by launch_youtab_desktop — out of
            // scope here). So this is a pure no-op on non-macOS.
            //
            // `--reinstall`/`--repair` opts out so a broken install can be
            // repaired by re-running setup instead of launching the bad app.
            if cfg!(target_os = "macos") && mode == AppMode::Install && !force_setup {
                let install_root = paths::youtab_home().join("youtab-agent-runtime");
                if bootstrap::youtab_is_installed(&install_root) {
                    match bootstrap::spawn_installed_desktop(&install_root) {
                        Ok(()) => {
                            // Brief grace so the spawned app is registered
                            // before we exit (mirrors launch_youtab_desktop).
                            std::thread::sleep(std::time::Duration::from_millis(200));
                            tracing::info!(
                                "youtab already installed — relaunched desktop; exiting installer"
                            );
                            app.handle().exit(0);
                            return Ok(());
                        }
                        Err(err) => {
                            tracing::warn!(
                                ?err,
                                "relaunch of installed desktop failed; showing installer UI"
                            );
                        }
                    }
                }
            }
            // First run / repair install, or Update mode: reveal the UI.
            match app.get_webview_window("main") {
                Some(win) => {
                    if let Err(err) = win.show() {
                        tracing::error!(?err, "failed to show main installer window");
                    }
                }
                None => {
                    tracing::error!("main installer window not found; installer UI will not appear");
                }
            }
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            // Mode (install vs update)
            get_mode,
            get_installation_kind,
            get_release_channel,
            // Bootstrap lifecycle
            bootstrap::start_bootstrap,
            bootstrap::cancel_bootstrap,
            bootstrap::get_bootstrap_status,
            // Update lifecycle
            update::start_update,
            // Hand-off
            bootstrap::launch_youtab_desktop,
            // Diagnostics
            paths::get_log_path,
            paths::get_youtab_home,
            paths::open_log_dir,
        ])
        .run(tauri::generate_context!())
        .expect("error while running Youtab Setup");
}

#[cfg(test)]
mod tests {
    use super::{force_setup_from_args, AppMode};

    #[test]
    fn installation_preview_preserves_unknown_or_partial_installs() {
        let directory = tempfile::tempdir().unwrap();
        let root = directory.path().join("runtime");
        assert_eq!(super::installation_kind(&root, true).unwrap(), "fresh");
        std::fs::create_dir(&root).unwrap();
        assert!(super::installation_kind(&root, true).is_err());
        let marker = root.join(".youtab-agent-runtime-bootstrap-complete");
        std::fs::create_dir(root.join(".git")).unwrap();
        std::fs::write(&marker, b"{}").unwrap();
        assert_eq!(super::installation_kind(&root, true).unwrap(), "legacy");
        assert!(super::installation_kind(&root, false).is_err());
        for bad in [serde_json::json!({"releaseSequence": true}), serde_json::json!({"releaseSequence": 0}), serde_json::json!({"artifactSha256": "partial"})] {
            std::fs::write(&marker, bad.to_string()).unwrap();
            assert!(super::installation_kind(&root, true).is_err());
        }
        std::fs::write(&marker, serde_json::json!({"releaseSequence": 3, "pinnedCommit": "a".repeat(40), "artifactSha256": "b".repeat(64), "releaseBaseUrl": "https://api.youtab.io/pilot-runtime-example/releases"}).to_string()).unwrap();
        assert_eq!(super::installation_kind(&root, true).unwrap(), "artifact");
    }

    #[test]
    fn bare_args_are_install() {
        assert_eq!(AppMode::from_args(Vec::<String>::new()), AppMode::Install);
        assert_eq!(AppMode::from_args(["--foo", "bar"]), AppMode::Install);
    }

    #[test]
    fn update_flag_selects_update() {
        assert_eq!(AppMode::from_args(["--update"]), AppMode::Update);
        assert_eq!(
            AppMode::from_args(["--something", "--update", "--else"]),
            AppMode::Update
        );
    }

    #[test]
    fn reinstall_and_repair_flags_force_setup() {
        assert!(force_setup_from_args(["--reinstall"]));
        assert!(force_setup_from_args(["--repair"]));
        assert!(force_setup_from_args(["--migrate-legacy"]));
        assert!(force_setup_from_args(["--foo", "--repair", "--bar"]));
    }

    #[test]
    fn bare_or_unrelated_args_do_not_force_setup() {
        assert!(!force_setup_from_args(Vec::<String>::new()));
        assert!(!force_setup_from_args(["--foo", "bar"]));
        // --update must not be mistaken for a force-setup flag.
        assert!(!force_setup_from_args(["--update"]));
    }

    #[test]
    fn force_setup_flags_do_not_affect_mode_selection() {
        // The repair flags must never flip Install<->Update.
        assert_eq!(AppMode::from_args(["--reinstall"]), AppMode::Install);
        assert_eq!(AppMode::from_args(["--repair"]), AppMode::Install);
        assert_eq!(
            AppMode::from_args(["--update", "--reinstall"]),
            AppMode::Update
        );
    }
}

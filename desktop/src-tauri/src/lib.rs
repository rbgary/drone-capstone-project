//! The desktop shell around the flight agent.
//!
//! The app's whole reason to exist is the constraint in CLAUDE.md: the
//! Crazyradio is a USB dongle, so only the machine holding it can command the
//! drone. This bundles the agent as a sidecar so an operator installs one
//! thing and never sees Python.
//!
//! Two decisions worth knowing about:
//!
//! **The sidecar is spawned from Rust, never from the webview.** Granting the
//! frontend `shell:allow-execute` would let any script in the window start a
//! process; the window has no such permission, and the only binary this app
//! can launch is the one compiled into `externalBin`.
//!
//! **The child is killed on exit, deliberately and on every path.** The agent
//! can have a drone armed. An orphaned agent holding the radio also blocks the
//! next launch, which presents as "no drone found" with a drone plainly
//! sitting there.

use std::sync::Mutex;

use uuid::Uuid;


use tauri::{Emitter, Manager, RunEvent, State};
use tauri_plugin_shell::process::{CommandChild, CommandEvent};
use tauri_plugin_shell::ShellExt;

/// The port the agent serves on. Matches `DEFAULT_PORT` in `api/rest.py`;
/// the agent binds localhost only.
const AGENT_PORT: u16 = 8765;

/// Which Supabase project the operator signs in to.
///
/// Compiled in rather than read from the environment: an installed `.app` is
/// launched by Finder with no shell profile, so an operator who has never seen
/// a terminal would get "sign-in is not configured" and no way to fix it.
///
/// The publishable key is public by design — every table is behind RLS and the
/// agent signs in as a real operator account (`sync/cloud.py`). **The secret
/// key must never appear here**: this binary ships inside an installer.
const SUPABASE_URL: &str = "https://tmswmgwomoabvftdreeb.supabase.co";
const SUPABASE_ANON_KEY: &str = "sb_publishable_cD47iXvjPbhUYH8SiYF97g_SzjUcPGw";

/// The compiled-in default, unless this machine sets one — which is how a
/// developer points the app at a local Supabase without a rebuild.
fn configured(name: &str, fallback: &str) -> String {
    std::env::var(name)
        .map(|value| value.trim().to_string())
        .ok()
        .filter(|value| !value.is_empty())
        .unwrap_or_else(|| fallback.to_string())
}

/// The running agent, so it can be stopped on exit.
#[derive(Default)]
struct AgentProcess(Mutex<Option<CommandChild>>);

/// Why the agent is not running, once it has stopped or failed to start.
///
/// Kept in state as well as emitted, because an agent that dies in its first
/// second dies before the window has registered a listener — and that is the
/// case the operator most needs explained. Before this, the reason was emitted
/// as `agent-log` lines nothing listened to, and the window could only say
/// "Could not reach the flight agent. Restart DroneDeck."
#[derive(Default)]
struct AgentExit(Mutex<Option<String>>);

/// Does this line of agent output report a failure? Python's log levels, a
/// PyInstaller bootloader error (`[PYI-1234:ERROR]`), or the last line of a
/// traceback (`ModuleNotFoundError: …`).
fn looks_like_error(line: &str) -> bool {
    if line.contains("ERROR") || line.contains("CRITICAL") {
        return true;
    }
    match line.trim_start().split_once(": ") {
        Some((head, _)) => {
            !head.is_empty()
                && !head.contains(char::is_whitespace)
                && (head.ends_with("Error") || head.ends_with("Exception"))
        }
        None => false,
    }
}

/// The longest agent line quoted back to the operator.
const QUOTE_MAX_CHARS: usize = 300;

/// One sentence saying why the agent stopped, for the window to show.
fn describe_exit(code: Option<i32>, last_error: Option<&str>) -> String {
    if let Some(line) = last_error {
        let lower = line.to_lowercase();
        // uvicorn's bind failure: errno 48/98 on macOS/Linux, WSA 10048 on
        // Windows. The window would otherwise be talking to whatever holds it.
        if lower.contains("address already in use")
            || lower.contains("10048")
            || lower.contains("only one usage of each socket address")
        {
            return format!(
                "Port {AGENT_PORT} is already in use — another copy of DroneDeck, or an agent \
                 started from a terminal, is running. Quit it, then reopen DroneDeck."
            );
        }
    }
    let how = code.map_or_else(|| "on a signal".to_string(), |c| format!("with exit code {c}"));
    let quoted = last_error.map(|line| {
        let cut: String = line.trim().chars().take(QUOTE_MAX_CHARS).collect();
        format!(" — {}", cut.trim_end_matches('.'))
    });
    format!(
        "The flight agent stopped {how}{}. Quit and reopen DroneDeck; if it stops again, \
         its log has the details.",
        quoted.unwrap_or_default()
    )
}

/// Where the agent writes its log. Mirrors `data_dir()` in the agent's
/// `paths.py` — the same override, the same folder per OS — so the path the
/// window shows is the file the agent actually wrote.
fn agent_data_dir() -> Option<std::path::PathBuf> {
    use std::path::PathBuf;
    let home = || {
        std::env::var_os(if cfg!(windows) { "USERPROFILE" } else { "HOME" }).map(PathBuf::from)
    };
    if let Ok(value) = std::env::var("DRONEDECK_DATA_DIR") {
        let value = value.trim();
        if !value.is_empty() {
            return match value.strip_prefix('~') {
                Some(rest) => home().map(|h| h.join(rest.trim_start_matches(['/', '\\']))),
                None => Some(PathBuf::from(value)),
            };
        }
    }
    let root = if cfg!(target_os = "macos") {
        home()?.join("Library").join("Application Support")
    } else if cfg!(windows) {
        match std::env::var_os("APPDATA") {
            Some(appdata) => PathBuf::from(appdata),
            None => home()?.join("AppData").join("Roaming"),
        }
    } else {
        match std::env::var_os("XDG_DATA_HOME") {
            Some(xdg) => PathBuf::from(xdg),
            None => home()?.join(".local").join("share"),
        }
    };
    // The agent's data folder (sign-in, sessions, logs — the DroneDeck rename
    // moved it from CropWatcher to DroneDeck on 2026-10-25; the agent's
    // dronedeck/paths.py carries an existing folder over on first run).
    Some(root.join("DroneDeck"))
}

/// Append a line to the shell's own log, beside the agent's.
///
/// The shell had no log at all: launched from Launchpad, whatever it or macOS
/// said went nowhere, and on an Intel Mac (2026-09-25) the only report was "the
/// icon is in the Dock and no window appears". This records what the window
/// actually was at launch and on every Dock click, so the next report carries
/// the answer. Capped: one launch's worth of lines, not a history.
fn shell_log(line: &str) {
    if let Some(dir) = agent_data_dir().map(|d| d.join("logs")) {
        if std::fs::create_dir_all(&dir).is_ok() {
            append_capped(&dir.join("shell.log"), line, SHELL_LOG_MAX_BYTES);
        }
    }
}

const SHELL_LOG_MAX_BYTES: u64 = 256 * 1024;

/// Append one timestamped line; start the file afresh once it passes `max`.
fn append_capped(path: &std::path::Path, line: &str, max: u64) {
    use std::io::Write;
    let too_big = std::fs::metadata(path).map(|m| m.len() > max).unwrap_or(false);
    let file = if too_big {
        std::fs::File::create(path)
    } else {
        std::fs::OpenOptions::new().create(true).append(true).open(path)
    };
    if let Ok(mut file) = file {
        let secs = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_secs())
            .unwrap_or(0);
        let _ = writeln!(file, "{secs} pid {} {line}", std::process::id());
    }
}

/// What the main window is, in numbers: visible, minimised, where, how big,
/// and on which display. "Where" is the question "no window appears" asks.
fn describe_window(window: &tauri::WebviewWindow) -> String {
    let monitor = window.current_monitor().ok().flatten().map_or_else(
        || "no display".to_string(),
        |m| format!("display {:?} at {:?} size {:?}", m.name(), m.position(), m.size()),
    );
    format!(
        "visible={:?} minimized={:?} position={:?} size={:?} {monitor}",
        window.is_visible().ok(),
        window.is_minimized().ok(),
        window.outer_position().ok().map(|p| (p.x, p.y)),
        window.outer_size().ok().map(|s| (s.width, s.height)),
    )
}

/// Show the main window, un-minimised and in front.
///
/// macOS convention (applicationShouldHandleReopen): clicking a running app's
/// Dock or Launchpad icon when it has no visible window brings one back.
/// Tauri reports the click as RunEvent::Reopen and does nothing by default, so
/// a window that was hidden, minimised or never ordered front stayed that way
/// however often the icon was clicked. Also called once the app is Ready.
fn bring_window_forward(app: &tauri::AppHandle, why: &str) {
    match app.get_webview_window("main") {
        Some(window) => {
            shell_log(&format!("{why}: before {}", describe_window(&window)));
            let _ = window.unminimize();
            let _ = window.show();
            let _ = window.set_focus();
            shell_log(&format!("{why}: after {}", describe_window(&window)));
        }
        None => shell_log(&format!("{why}: there is no main window")),
    }
}

/// Record why the agent is not running, and tell the window.
fn agent_stopped(app: &tauri::AppHandle, reason: String) {
    app.state::<AgentExit>().0.lock().unwrap().replace(reason.clone());
    let _ = app.emit("agent-exit", reason);
}

/// The local control token.
///
/// Binding the agent to localhost keeps other machines out but not other
/// programs or web pages on this one, and its API can arm a drone. This token
/// is generated per launch, handed to the agent in its environment, and given
/// to this window through `agent_token` — a page on the internet cannot know
/// it, so it cannot fly the drone.
struct ControlToken(String);

fn new_token() -> String {
    // Random, not derived from the clock or the process id: a local page that
    // could guess those could fly the drone.
    Uuid::new_v4().simple().to_string()
}

/// A line of agent output, forwarded to the log panel.
#[derive(Clone, serde::Serialize)]
struct LogLine {
    stream: &'static str,
    line: String,
}

#[tauri::command]
fn agent_port() -> u16 {
    AGENT_PORT
}

#[tauri::command]
fn agent_token(token: State<'_, ControlToken>) -> String {
    token.0.clone()
}

/// Why the agent is not running; None while it runs.
#[tauri::command]
fn agent_exit(exit: State<'_, AgentExit>) -> Option<String> {
    exit.0.lock().unwrap().clone()
}

/// The agent's log file, for the window to point at when something failed.
#[tauri::command]
fn agent_log_path() -> Option<String> {
    agent_data_dir().map(|dir| dir.join("logs").join("agent.log").display().to_string())
}

/// Stop the motors, whatever is happening.
///
/// Kept in Rust as a last resort: it reaches the agent's REST endpoint even if
/// the window's own fetch layer is wedged. The normal path is the window's
/// hold-to-activate Emergency stop, which calls `/session/emergency-stop`.
#[tauri::command]
async fn stop_motors(token: State<'_, ControlToken>) -> Result<(), String> {
    let url = format!("http://127.0.0.1:{AGENT_PORT}/session/emergency-stop");
    let response = tauri_plugin_http::reqwest::Client::new()
        .post(&url)
        .header("X-Agent-Token", token.0.clone())
        .send()
        .await
        .map_err(|e| format!("could not reach the agent: {e}"))?;

    if !response.status().is_success() {
        return Err(format!("the agent refused the stop: HTTP {}", response.status()));
    }
    Ok(())
}

/// Start the bundled agent and stream its output into the log panel.
fn spawn_agent(app: &tauri::AppHandle) -> Result<(), String> {
    let token = app.state::<ControlToken>().0.clone();
    let (mut rx, child) = app
        .shell()
        .sidecar("dronedeck-agent")
        .map_err(|e| format!("the bundled agent is missing: {e}"))?
        .env("DRONEDECK_AGENT_TOKEN", token)
        .env("SUPABASE_URL", configured("SUPABASE_URL", SUPABASE_URL))
        .env("SUPABASE_ANON_KEY", configured("SUPABASE_ANON_KEY", SUPABASE_ANON_KEY))
        // `--exit-with-parent` is what actually stops the agent. Killing the
        // child below only reaches PyInstaller's bootloader; the real Python
        // process is its child and survives, orphaned onto launchd, still
        // holding the radio. Measured: desktop → bootloader → python, and the
        // last one owns the port. Closing stdin is the signal that reaches it.
        .args(["serve", "--port", &AGENT_PORT.to_string(), "--exit-with-parent"])
        .spawn()
        .map_err(|e| format!("could not start the agent: {e}"))?;

    app.state::<AgentProcess>().0.lock().unwrap().replace(child);

    // The agent's own log is the only account of what the drone did. Forward
    // it to the window rather than dropping it: on a failed flight it is the
    // first thing anyone asks for, and a bundled app has no terminal.
    let handle = app.clone();
    tauri::async_runtime::spawn(async move {
        // The newest line that reads as a failure: when the agent dies, it is
        // usually the reason (a traceback's last line, uvicorn's bind error).
        let mut last_error: Option<String> = None;
        while let Some(event) = rx.recv().await {
            let line = match event {
                CommandEvent::Stdout(bytes) => Some(("stdout", bytes)),
                CommandEvent::Stderr(bytes) => Some(("stderr", bytes)),
                CommandEvent::Terminated(status) => {
                    let reason = describe_exit(status.code, last_error.as_deref());
                    let _ = handle.emit(
                        "agent-log",
                        LogLine { stream: "stderr", line: reason.clone() },
                    );
                    agent_stopped(&handle, reason);
                    None
                }
                _ => None,
            };

            if let Some((stream, bytes)) = line {
                let text = String::from_utf8_lossy(&bytes).trim_end().to_string();
                if looks_like_error(&text) {
                    last_error = Some(text.clone());
                }
                let _ = handle.emit("agent-log", LogLine { stream, line: text });
            }
        }
    });

    Ok(())
}

/// Stop the agent. Safe to call more than once.
fn stop_agent(state: &State<'_, AgentProcess>) {
    if let Some(child) = state.0.lock().unwrap().take() {
        let _ = child.kill();
    }
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_http::init())
        .plugin(tauri_plugin_notification::init())
        .manage(AgentProcess::default())
        .manage(AgentExit::default())
        .manage(ControlToken(new_token()))
        .invoke_handler(tauri::generate_handler![
            agent_port,
            agent_token,
            agent_exit,
            agent_log_path,
            stop_motors,
        ])
        .setup(|app| {
            if let Err(message) = spawn_agent(app.handle()) {
                // A window that silently has no agent behind it is worse than
                // one that says so: the operator would read an empty status
                // panel as "idle" rather than "not running". This runs before
                // the window exists, so it is kept in state for it to ask.
                let _ = app.handle().emit(
                    "agent-log",
                    LogLine {
                        stream: "stderr",
                        line: message.clone(),
                    },
                );
                agent_stopped(
                    app.handle(),
                    format!("The flight agent could not start ({message}). Reinstall DroneDeck."),
                );
            }
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while running tauri application")
        .run(|app, event| match event {
            RunEvent::Ready => bring_window_forward(app, "ready"),
            // The Dock or Launchpad icon clicked while running (macOS only).
            #[cfg(target_os = "macos")]
            RunEvent::Reopen { has_visible_windows, .. } => {
                bring_window_forward(app, &format!("reopen (visible windows: {has_visible_windows})"));
            }
            // Covers the window close, the Quit menu item and a signal alike.
            RunEvent::ExitRequested { .. } | RunEvent::Exit => {
                stop_agent(&app.state::<AgentProcess>());
            }
            _ => {}
        });
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn error_lines_are_recognised() {
        assert!(looks_like_error("2026-09-25 10:00:00 ERROR dronedeck: boom"));
        assert!(looks_like_error("ERROR:    [Errno 48] error while attempting to bind"));
        assert!(looks_like_error("2026-09-25 CRITICAL dronedeck: unhandled exception"));
        assert!(looks_like_error("[PYI-4242:ERROR] Failed to execute script 'sidecar'"));
        assert!(looks_like_error("ModuleNotFoundError: No module named 'usb'"));
        assert!(looks_like_error("  OSError: [Errno 30] Read-only file system"));
    }

    #[test]
    fn ordinary_lines_are_not_errors() {
        assert!(!looks_like_error("2026-09-25 INFO dronedeck: agent API on 127.0.0.1:8765"));
        assert!(!looks_like_error("INFO:     Uvicorn running on http://127.0.0.1:8765"));
        assert!(!looks_like_error("Looking for devices...."));
        assert!(!looks_like_error("the camera: no picture yet"));
        assert!(!looks_like_error(""));
    }

    #[test]
    fn a_port_taken_on_macos_or_linux_names_the_port() {
        let line = "ERROR:    [Errno 48] error while attempting to bind on address \
                    ('127.0.0.1', 8765): address already in use";
        let reason = describe_exit(Some(1), Some(line));
        assert!(reason.contains("Port 8765 is already in use"), "{reason}");
        assert!(reason.contains("another copy of DroneDeck"));
    }

    #[test]
    fn a_port_taken_on_windows_names_the_port() {
        let line = "ERROR:    [Errno 10048] error while attempting to bind on address \
                    ('127.0.0.1', 8765): only one usage of each socket address \
                    (protocol/network address/port) is normally permitted";
        assert!(describe_exit(Some(1), Some(line)).contains("Port 8765 is already in use"));
    }

    #[test]
    fn a_crash_quotes_its_last_error_and_code() {
        let reason = describe_exit(Some(1), Some("ModuleNotFoundError: No module named 'usb'"));
        assert_eq!(
            reason,
            "The flight agent stopped with exit code 1 — ModuleNotFoundError: No module \
             named 'usb'. Quit and reopen DroneDeck; if it stops again, its log has the details."
        );
    }

    #[test]
    fn a_quoted_line_ending_in_a_full_stop_does_not_get_two() {
        let reason = describe_exit(Some(1), Some("OSError: disk full."));
        assert!(reason.contains("disk full. Quit"), "{reason}");
    }

    #[test]
    fn an_exit_with_no_error_line_still_says_how() {
        assert!(describe_exit(Some(0), None)
            .starts_with("The flight agent stopped with exit code 0. Quit and reopen"));
        assert!(describe_exit(None, None).starts_with("The flight agent stopped on a signal. "));
    }

    #[test]
    fn a_long_line_is_cut_on_a_character_boundary() {
        let line = format!("ValueError: {}", "é".repeat(1000));
        let reason = describe_exit(Some(1), Some(&line));
        let quoted = reason.split(" — ").nth(1).unwrap().split(". Quit").next().unwrap();
        assert_eq!(quoted.chars().count(), QUOTE_MAX_CHARS);
    }

    #[test]
    fn the_shell_log_appends_and_starts_afresh_past_its_cap() {
        let dir = std::env::temp_dir().join(format!("cw-shell-log-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("shell.log");
        let _ = std::fs::remove_file(&path);
        append_capped(&path, "ready: before visible=Some(false)", 1024);
        append_capped(&path, "ready: after visible=Some(true)", 1024);
        let text = std::fs::read_to_string(&path).unwrap();
        assert_eq!(text.lines().count(), 2, "{text}");
        assert!(text.contains("after visible=Some(true)"));
        std::fs::write(&path, "x".repeat(2048)).unwrap();
        append_capped(&path, "reopen", 1024);
        let text = std::fs::read_to_string(&path).unwrap();
        assert_eq!(text.lines().count(), 1, "the oversized file was not started afresh");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn the_log_path_honours_the_override_like_the_agent() {
        // One test owns this variable; nothing else in the suite reads it.
        std::env::set_var("DRONEDECK_DATA_DIR", "/tmp/cw-data");
        assert_eq!(agent_data_dir(), Some(std::path::PathBuf::from("/tmp/cw-data")));
        std::env::remove_var("DRONEDECK_DATA_DIR");
        let default = agent_data_dir().expect("a home directory");
        assert!(default.ends_with("DroneDeck"), "{}", default.display());
    }
}

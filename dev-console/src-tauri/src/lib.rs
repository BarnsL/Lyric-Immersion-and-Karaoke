// Minimal Tauri 2 shell. The frontend does everything meaningful; the Rust
// side only bootstraps the runtime + wires the opener plugin (used by the
// Resources tab to open external URLs and the Explorer view of worktrees).
use tauri::Manager;

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        // TICKET-198: exactly one console, ever. Two windows open at once means
        // two different builds side by side showing different numbers and no way
        // to tell which is live — precisely how a stale console got mistaken for
        // the real one. A second launch hands its argv to the instance already
        // running and exits; here we just surface that window.
        //
        // NB: register this FIRST. The plugin decides whether this process is the
        // primary instance during init, and anything registered ahead of it would
        // be doing setup work in a process that is about to exit.
        .plugin(tauri_plugin_single_instance::init(|app, _argv, _cwd| {
            // tauri.conf.json declares no explicit window `label`, so Tauri names
            // it "main" by default — but relying on that would break silently if
            // the config ever gains a label. Fall back to whatever window exists.
            let w = app
                .get_webview_window("main")
                .or_else(|| app.webview_windows().values().next().cloned());
            if let Some(w) = w {
                // Un-minimise before focusing: a minimised window silently
                // refuses set_focus, so without this a second launch would look
                // like nothing happened at all.
                let _ = w.unminimize();
                let _ = w.show();
                let _ = w.set_focus();
            }
        }))
        .plugin(tauri_plugin_opener::init())
        .run(tauri::generate_context!())
        .expect("error while running Lyric Immersion Dev Console");
}

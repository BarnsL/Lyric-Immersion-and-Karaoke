// Minimal Tauri 2 shell. The frontend does everything meaningful; the Rust
// side only bootstraps the runtime + wires the opener plugin (used by the
// Resources tab to open external URLs and the Explorer view of worktrees).
#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .run(tauri::generate_context!())
        .expect("error while running Lyric Immersion Dev Console");
}

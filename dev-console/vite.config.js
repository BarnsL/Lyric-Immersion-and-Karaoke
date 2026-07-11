import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
// Tauri 2 dev-server contract:
//  - fixed port 1420 (matches tauri.conf.json.build.devUrl)
//  - listen on 0.0.0.0 so the WebView can reach it via a stable host
//  - HMR uses a secondary port so it doesn't conflict with Tauri's own IPC
export default defineConfig({
    plugins: [react()],
    clearScreen: false,
    server: {
        port: 1420,
        strictPort: true,
        host: "127.0.0.1",
        hmr: { protocol: "ws", host: "127.0.0.1", port: 1421 },
        watch: { ignored: ["**/src-tauri/**"] },
    },
    envPrefix: ["VITE_", "TAURI_"],
    build: {
        target: "chrome110",
        minify: "esbuild",
        sourcemap: false,
        chunkSizeWarningLimit: 900,
    },
});

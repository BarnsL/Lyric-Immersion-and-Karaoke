import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Tauri 2 dev-server contract:
//  - fixed port 1420 (matches tauri.conf.json.build.devUrl)
//  - bind 127.0.0.1 (loopback only). The comment here used to say 0.0.0.0, which
//    was never what the code did and would have exposed the dev server to the
//    local network.
//  - HMR uses a secondary port so it doesn't conflict with Tauri's own IPC.
//    That port MUST also be listed in tauri.conf.json's `devCsp`, or the CSP
//    blocks the HMR websocket and edits appear to require a full rebuild even
//    under `npm run tauri:dev`. See docs/DEV_CONSOLE.md.
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

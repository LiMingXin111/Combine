import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
export default defineConfig({
  plugins: [react()],
  server: {
    host: "127.0.0.1",
    port: 1420,
    strictPort: true,
    watch: {
      ignored: ["**/src-tauri/**", "**/.runtime/**", "**/.ai-smoke/**", "**/.dev/**"],
    },
    proxy: {
      "/desktop-rpc": {
        target: "http://127.0.0.1:1421",
        changeOrigin: true,
        rewrite: () => "/rpc",
      },
    },
  },
  build: { target: "es2022" },
});

import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// `npm run dev` serves the UI on :5173 with hot reload and forwards API,
// login and WebSocket calls to the bot's dashboard server (WEB_PORT, 8081).
const BOT = process.env.BOT_URL ?? "http://127.0.0.1:8081";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api/ws": { target: BOT, ws: true },
      "/api": BOT,
      "/auth": BOT,
    },
  },
  build: { outDir: "dist", sourcemap: false },
});

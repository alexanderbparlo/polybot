import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    // TODO: proxy /api/* to FastAPI WebSocket bridge when live data is wired
    // proxy: { "/api": "http://localhost:8000" }
  },
});

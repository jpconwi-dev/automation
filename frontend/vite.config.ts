import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// In dev, /api is proxied to the FastAPI backend, so the browser never needs to know its address.
export default defineConfig({
  plugins: [react()],
  server: { port: 5173, proxy: { "/api": process.env.VITE_BACKEND_URL ?? "http://127.0.0.1:8000" } },
});

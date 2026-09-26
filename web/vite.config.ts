import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// `npm run build` writes into the Python package so `deeptrace serve` can serve it at /.
// `npm run build:demo` builds the static replay demo (for GitHub Pages).
const demo = process.env.VITE_DEMO === "1";

export default defineConfig({
  plugins: [react()],
  base: process.env.VITE_BASE ?? "/",
  build: { outDir: demo ? "dist-demo" : "../src/deeptrace_agent/web_dist", emptyOutDir: true },
  server: {
    proxy: Object.fromEntries(
      ["/runs", "/metrics", "/health"].map((path) => [path, "http://127.0.0.1:8000"]),
    ),
  },
});

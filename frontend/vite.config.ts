import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// The SPA is built into the Django app's static dir and served by
// bi_admin.views.spa, which reads .vite/manifest.json to find the bundle.
export default defineConfig({
  plugins: [react()],
  base: process.env.GOZAR_STATIC_BASE ?? "/static/bi_admin/spa/",
  build: {
    outDir: "../src/bi_admin/static/bi_admin/spa",
    emptyOutDir: true,
    manifest: true,
    // The Reports chunk (ECharts) is ~580 kB but only loaded on /reports.
    chunkSizeWarningLimit: 700,
  },
  server: {
    port: 5173,
    strictPort: true,
    cors: true,
    origin: "http://localhost:5173",
  },
});

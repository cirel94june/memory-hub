import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  base: "/app/",
  server: {
    host: "0.0.0.0",
    port: 5173,
    proxy: {
      "/api": "http://localhost:8888",
      "/v1": "http://localhost:8888",
    },
  },
  build: {
    outDir: "../static-app",
    emptyOutDir: true,
  },
});

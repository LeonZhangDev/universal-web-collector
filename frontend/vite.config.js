import vue from "@vitejs/plugin-vue";
import { defineConfig } from "vite";

// 端口与代理目标由 scripts/start.py 注入, 与后端实际端口自动对齐
const backend = `http://127.0.0.1:${process.env.UWC_PORT || "8000"}`;
const frontPort = Number(process.env.UWC_FRONT_PORT || 5173);

export default defineConfig({
  plugins: [vue()],
  server: {
    port: frontPort,
    strictPort: true,
    proxy: {
      "/tasks": backend,
      "/collectors": backend,
      "/events": backend,
      "/files": backend,
      "/healthz": backend,
    },
  },
});

import react, { reactCompilerPreset } from "@vitejs/plugin-react";
import babel from "@rolldown/plugin-babel";
import { defineConfig } from "vite";

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), babel({ presets: [reactCompilerPreset()] })],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        // 127.0.0.1 而非 localhost：后端 HOST=0.0.0.0 只监听 IPv4，localhost 会被
        // 双栈解析成 ::1 + 127.0.0.1（Node Happy Eyeballs 先试 ::1），两路全拒时
        // 报 AggregateError ECONNREFUSED；显式走 IPv4 消除地址族歧义。
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
        // 不做 rewrite：后端 API_V1_PREFIX=/api/v1，/api 前缀原样转发即与路由对齐。
        //（原 rewrite 的 /^\/api^\/v1/ 正则永不匹配，属死代码，已删除）
      },
    },
  },
});

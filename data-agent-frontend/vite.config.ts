import { fileURLToPath, URL } from 'node:url'

import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vite'

// https://vite.dev/config/
export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    // 严格固定端口：conf/app_config.yaml 的 cors_origins 白名单与本端口一一对应，
    // 若允许自动换端口，实际来源会与白名单不一致且难以察觉
    port: 5173,
    strictPort: true,
    // 开发期代理到后端；生产由 Nginx 承担同样的转发（TDD 14.1），
    // 并在 Nginx 侧关闭 SSE 缓冲
    proxy: {
      '/api': { target: 'http://127.0.0.1:8000', changeOrigin: true },
      '/health': { target: 'http://127.0.0.1:8000', changeOrigin: true },
    },
  },
})

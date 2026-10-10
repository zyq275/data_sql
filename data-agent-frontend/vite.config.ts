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
  build: {
    rollupOptions: {
      output: {
        // 把两个大依赖单独成块。
        //
        // 不拆的话打包器会按"谁先用到"把它们塞进某个页面块里——实测 echarts 被塞进了
        // 评测看板的块，于是打开问答页要先下载评测看板那一份。拆开之后：
        //   1. 依赖块只在内容真的变化时失效，改业务代码不会让用户重下 800KB；
        //   2. echarts 只在用到图表的页面被拉取，纯表格页面不必为它买单。
        // 按模块路径分类，而不是按包名列表：rolldown 只接受函数形式，
        // 并且要连 zrender（echarts 的渲染层依赖）一起归入 echarts 块，
        // 否则它会被当成一个"谁先用到就归谁"的散包，拆分就白做了。
        manualChunks(id: string) {
          if (id.includes('node_modules/echarts') || id.includes('node_modules/zrender')) {
            return 'echarts'
          }
          if (
            id.includes('node_modules/element-plus') ||
            id.includes('node_modules/@element-plus')
          ) {
            return 'element-plus'
          }
          return undefined
        },
      },
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

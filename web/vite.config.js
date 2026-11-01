import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  // main.js 在 createApp 前用顶层 await 拉取认证配置,需要 esnext 目标(支持 top-level await)。
  // 该 Dashboard 面向现代浏览器,esnext 输出无兼容问题。
  build: {
    target: 'esnext',
  },
  optimizeDeps: {
    esbuildOptions: { target: 'esnext' },
  },
  server: {
    port: 5466,
    host: true,
    // 允许通过 cpolar 等隧道域名访问开发服务器
    // 如需更严格，可改为具体域名数组，如 ['225da598.r10.vip.cpolar.cn']
    allowedHosts: ['.cpolar.cn'],
    proxy: {
      '/extract': {
        target: 'http://localhost:8036',
        changeOrigin: true,
      },
      '/tasks': {
        target: 'http://localhost:8036',
        changeOrigin: true,
      },
      '/stats': {
        target: 'http://localhost:8036',
        changeOrigin: true,
      },
      '/models': {
        target: 'http://localhost:8036',
        changeOrigin: true,
      },
      '/manuals': {
        target: 'http://localhost:8036',
        changeOrigin: true,
      },
      '/events': {
        target: 'http://localhost:8036',
        changeOrigin: true,
      },
      '/api': {
        target: 'http://localhost:8036',
        changeOrigin: true,
      },
      '/callback': {
        target: 'http://localhost:8036',
        changeOrigin: true,
      },
      '/ti': {
        target: 'http://localhost:8036',
        changeOrigin: true,
        ws: true,  // 远程登录桌面（noVNC websockify）走 WebSocket
      },
      '/server-info': {
        target: 'http://localhost:8036',
        changeOrigin: true,
      },
      '/auth': {
        target: 'http://localhost:8036',
        changeOrigin: true,
      },
      '/admin': {
        target: 'http://localhost:8036',
        changeOrigin: true,
      },
    },
  },
})

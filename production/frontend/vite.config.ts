import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { fileURLToPath, URL } from 'node:url'

const apiTarget = process.env.AVATAR_STUDIO_API_TARGET || 'http://127.0.0.1:8000'

// 开发期通过 Vite 代理访问 FastAPI，避免浏览器直连时的跨域配置。
export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: apiTarget,
        changeOrigin: true,
      },
      '/media': {
        target: apiTarget,
        changeOrigin: true,
      },
    },
  },
})

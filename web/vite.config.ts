import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// Vite 配置(dev/build)。Vitest 配置见独立的 vitest.config.ts。
// /api 反代到本地 FastAPI(dev 时前端直连后端)。
export default defineConfig({
  plugins: [vue()],
  server: {
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
})

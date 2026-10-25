import { defineConfig } from 'vitest/config'
import vue from '@vitejs/plugin-vue'

// Vitest 独立配置:与 vite.config.ts 分离,避免 vitest 内嵌 vite 与顶层 vite 的类型/版本冲突。
export default defineConfig({
  plugins: [vue()],
  test: {
    environment: 'jsdom',
    globals: true,
  },
})

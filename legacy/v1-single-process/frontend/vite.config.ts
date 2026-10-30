import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 第一阶段：/api 代理到后端 8000。第二阶段改为指向网关 9000。
export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
    },
  },
})

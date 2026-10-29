import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 第二阶段：前端**只认 9000**（协议层）。
// 对话、ASR、TTS、余额全部走同一个出口，前端代码里不出现第二个地址。
export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:9000',
        changeOrigin: true,
      },
    },
  },
})

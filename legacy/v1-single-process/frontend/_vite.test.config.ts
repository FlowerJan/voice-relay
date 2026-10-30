// 仅用于 DOM 级组件验证的临时构建配置（vite build --config _vite.test.config.ts）
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  build: {
    lib: {
      entry: 'dom_check.ts',
      formats: ['es'],
      fileName: 'dom_check',
    },
    outDir: '_domtest',
    target: 'esnext',
    rollupOptions: {
      external: ['jsdom', 'vue'],
    },
    minify: false,
    emptyOutDir: true,
  },
})

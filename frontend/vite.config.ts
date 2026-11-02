import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import { defineConfig } from 'vite'

// https://vite.dev/config/
// Phase 5：接入 Tailwind v4 插件，并将 /api 反向代理到后端 FastAPI（8000 端口），
// 使前端开发环境无需处理跨域即可调用后端 REST / SSE 接口。
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
    },
  },
})

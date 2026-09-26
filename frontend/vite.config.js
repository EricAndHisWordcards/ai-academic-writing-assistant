import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  // 打包态由 Electron 用 file:// 加载 dist/index.html，默认的 base='/' 会把资源
  // 写成绝对路径 /assets/xxx.js，在 file:// 下解析成 file:///assets/... ⇒ 404 ⇒
  // 窗口白屏（且因为 renderer 报错不出现在主进程日志里，很难查）。
  // './' 让资源引用变成相对路径，dev 与打包两种加载方式都对。
  base: './',
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
})

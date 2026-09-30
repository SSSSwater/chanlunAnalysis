import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  base: process.env.VITE_BASE_PATH || '/',
  plugins: [vue()],
  build: {
    // Split 3rd-party libs into cacheable vendor chunks (parallel download,
    // long-term caching) instead of one giant bundle.
    rollupOptions: {
      output: {
        manualChunks: {
          'vendor-vue': ['vue'],
          'vendor-element': ['element-plus'],
          'vendor-chart': ['chart.js', 'chartjs-chart-financial'],
          'vendor-ui': ['lucide-vue-next', 'axios'],
        },
      },
    },
    chunkSizeWarningLimit: 900,
  },
  server: {
    host: '127.0.0.1',
    port: 5173,
    strictPort: true,
    // Allow the public tunnel hostname so cloudflared can reach the dev server.
    allowedHosts: ['jiaren.aqhxx.top'],
  },
})

import { fileURLToPath, URL } from 'node:url'

import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vite'
import { VitePWA } from 'vite-plugin-pwa'

const apiProxy = process.env.VITE_API_PROXY ?? 'http://127.0.0.1:8000'

export default defineConfig({
  plugins: [
    vue(),
    VitePWA({
      registerType: 'autoUpdate',
      injectRegister: 'auto',
      includeAssets: [
        'icons/apple-touch-icon.png',
        'icons/favicon-32.png',
      ],
      manifest: {
        id: '/',
        name: 'CineForge AI 短剧创作',
        short_name: 'CineForge',
        description: '面向短剧生产的 AI 创作工作台',
        lang: 'zh-CN',
        start_url: '/workspace',
        scope: '/',
        display: 'standalone',
        background_color: '#f5f6f8',
        theme_color: '#0b211b',
        orientation: 'any',
        categories: ['productivity', 'photo', 'video'],
        icons: [
          {
            src: '/icons/app-icon-192.png',
            sizes: '192x192',
            type: 'image/png',
            purpose: 'any',
          },
          {
            src: '/icons/app-icon-512.png',
            sizes: '512x512',
            type: 'image/png',
            purpose: 'any',
          },
          {
            src: '/icons/app-icon-maskable-512.png',
            sizes: '512x512',
            type: 'image/png',
            purpose: 'maskable',
          },
        ],
      },
      workbox: {
        cleanupOutdatedCaches: true,
        navigateFallback: '/index.html',
        navigateFallbackDenylist: [/^\/api\//, /^\/uploads\//, /^\/health$/],
        globPatterns: ['**/*.{js,css,html,png,webp,jpg,svg,woff2}'],
      },
    }),
  ],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    host: '0.0.0.0',
    port: 5173,
    strictPort: true,
    proxy: {
      '/api': apiProxy,
      '/health': apiProxy,
      '/uploads': apiProxy,
    },
  },
})

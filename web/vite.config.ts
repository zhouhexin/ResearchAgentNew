import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
export default defineConfig({plugins:[vue()],server:{port:5173,proxy:{'/api':{target:'http://127.0.0.1:8001',changeOrigin:true}}},build:{chunkSizeWarningLimit:1100,rollupOptions:{output:{manualChunks:{ui:['element-plus'],vendor:['vue','vue-router','pinia','axios']}}}}})

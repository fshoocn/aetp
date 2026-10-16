import { fileURLToPath, URL } from 'node:url';

import { PrimeVueResolver } from '@primevue/auto-import-resolver';
import tailwindcss from '@tailwindcss/vite';
import vue from '@vitejs/plugin-vue';
import Components from 'unplugin-vue-components/vite';
import { defineConfig } from 'vite';

// https://vitejs.dev/config/
export default defineConfig({
    base: '/ui-static/',
    build: {
        outDir: fileURLToPath(new URL('../static/ui/dist', import.meta.url)),
        emptyOutDir: true
    },
    // 开发态把后端接口代理给本机节点（/ui-static 是 vite 自己的产物不代理；
    // 生产由后端同源托管，无需代理）
    server: {
        proxy: {
            '/api': 'http://127.0.0.1:8080',
            '/plugin-ui': 'http://127.0.0.1:8080'
        }
    },
    optimizeDeps: {
        noDiscovery: true
    },
    plugins: [
        vue(),
        tailwindcss(),
        Components({
            resolvers: [PrimeVueResolver()]
        })
    ],
    resolve: {
        alias: {
            '@': fileURLToPath(new URL('./src', import.meta.url))
        }
    },
    css: {
        preprocessorOptions: {
            scss: {
                api: 'modern-compiler'
            }
        }
    }
});

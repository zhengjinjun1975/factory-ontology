import { defineConfig } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

export default defineConfig({
  plugins: [svelte()],
  // 无独立静态资源目录，禁用 publicDir 避免与 outDir 冲突
  publicDir: false,
  build: {
    outDir: 'public',
    emptyOutDir: true,
  },
});

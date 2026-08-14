import { defineConfig } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

export default defineConfig({
  plugins: [svelte({
    // 过滤未使用 CSS 选择器警告(历史遗留死CSS, 非致命; 保留样式避免破坏scoped块)
    onwarn(warning, handler) {
      if (warning.code === 'css_unused_selector') return;
      handler(warning);
    },
  })],
  // 无独立静态资源目录，禁用 publicDir 避免与 outDir 冲突
  publicDir: false,
  build: {
    outDir: 'public',
    emptyOutDir: true,
  },
});

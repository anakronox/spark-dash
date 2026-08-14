import { defineConfig } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

export default defineConfig({
  plugins: [svelte()],
  build: {
    // Served by the backend container from /app/static.
    outDir: 'dist',
    emptyOutDir: true,
  },
  server: {
    // In development Vite serves the UI and proxies data to a running backend,
    // so the app is same-origin in dev exactly as it is in production — no
    // CORS config that only exists for local work.
    proxy: {
      '/api': { target: 'http://localhost:8080', changeOrigin: true },
      '/health': { target: 'http://localhost:8080', changeOrigin: true },
      '/ws': { target: 'ws://localhost:8080', ws: true },
    },
  },
});

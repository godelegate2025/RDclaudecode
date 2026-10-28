// The new desk, built into dist/ and served by the FastAPI desk at /desk-next.
// `npm run dev` proxies the API to a desk running locally on port 8010
// (the "care-desk-local" launch config), so sign-in and data are the real ones.
import react from '@vitejs/plugin-react';
import { defineConfig } from 'vite';

const desk = 'http://127.0.0.1:8010';

export default defineConfig({
  base: '/desk-next/',
  plugins: [react()],
  server: {
    port: 5173,
    proxy: Object.fromEntries(['/api', '/auth', '/static', '/login', '/welcome', '/favicon.ico']
      .map(p => [p, { target: desk, changeOrigin: false }])),
  },
  build: { outDir: 'dist', emptyOutDir: true, sourcemap: false },
});

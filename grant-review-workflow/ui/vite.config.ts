/// <reference types="vitest/config" />
import react from '@vitejs/plugin-react';
import { defineConfig } from 'vite';

export default defineConfig({
  plugins: [react()],
  base: './', // served from a bucket behind CloudFront (stage 5): relative asset paths
  server: {
    // `npm run dev:api` (VITE_API_URL=/api/): the local API (tests/local_api.py) on the same origin, so no CORS.
    proxy: { '/api': { target: 'http://127.0.0.1:8000', rewrite: (path) => path.replace(/^\/api/, '') } },
  },
  test: {
    environment: 'jsdom',
    globals: true, // test, expect and Testing Library's cleanup after each test
    setupFiles: ['./src/test/setup.ts'],
  },
});

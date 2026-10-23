/// <reference types="vitest/config" />
import react from '@vitejs/plugin-react';
import { defineConfig } from 'vite';

export default defineConfig({
  plugins: [react()],
  base: './', // served from a bucket behind CloudFront (stage 5): relative asset paths
  test: {
    environment: 'jsdom',
    globals: true, // test, expect and Testing Library's cleanup after each test
    setupFiles: ['./src/test/setup.ts'],
  },
});

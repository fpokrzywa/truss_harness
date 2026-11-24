import react from '@vitejs/plugin-react';
import { defineConfig } from 'vitest/config';

// The generated app's bundle (design SP5 6.5): relative base and fixed, unhashed file names, so
// two builds of the same sources give byte-identical files (scripts/appgen_web.py --check).
export default defineConfig({
  plugins: [react()],
  base: './',
  build: {
    sourcemap: false,
    rollupOptions: {
      output: {
        entryFileNames: 'assets/[name].js',
        chunkFileNames: 'assets/[name].js',
        assetFileNames: 'assets/[name][extname]',
      },
    },
  },
  test: {
    environment: 'jsdom',
    setupFiles: ['./src/test/setup.ts'],
    include: ['src/**/*.test.{ts,tsx}'],
    css: false,
  },
});

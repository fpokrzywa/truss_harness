import type { NextConfig } from 'next';

const config: NextConfig = {
  // Self-contained server bundle for native deployment: `node .next/standalone/server.js`.
  output: 'standalone',
  reactStrictMode: true,
  poweredByHeader: false,
  // Lets two dev servers run side by side without sharing `.next`.
  distDir: process.env.SHIFDE_NEXT_DIST_DIR?.trim() || undefined,
  // `/api/*` is proxied at request time by src/proxy.ts (SHIFDE_API_ORIGIN is a runtime setting).
  // A `rewrites()` destination would be baked into the build, so none is declared here.
};

export default config;

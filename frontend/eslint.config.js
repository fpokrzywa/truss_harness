import nextVitals from 'eslint-config-next/core-web-vitals';
import nextTs from 'eslint-config-next/typescript';

const config = [
  ...nextVitals,
  ...nextTs,
  {
    ignores: [
      '.next/',
      '.next-demo/',
      '.next-smoke/',
      '.next-e2e/',
      'test-results/',
      'playwright-report/',
      'node_modules/',
      'next-env.d.ts',
      'coverage/',
      'appgen-web/',
    ],
  },
];

export default config;

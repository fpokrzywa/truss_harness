import { afterEach, describe, expect, it, vi } from 'vitest';

describe('next.config distDir', () => {
  afterEach(() => { vi.unstubAllEnvs(); vi.resetModules(); });
  it('defaults to .next', async () => {
    const { default: cfg } = await import('../next.config');
    expect(cfg.distDir ?? '.next').toBe('.next');
  });
  it('uses SHIFDE_NEXT_DIST_DIR when set', async () => {
    vi.stubEnv('SHIFDE_NEXT_DIST_DIR', '.next-demo');
    const { default: cfg } = await import('../next.config');
    expect(cfg.distDir).toBe('.next-demo');
  });
});

import { describe, expect, it } from 'vitest';
import { platformLabel } from './platforms';

describe('platformLabel', () => {
  it.each([
    ['databricks', 'Databricks'],
    ['snowflake', 'Snowflake'],
    ['fabric', 'Microsoft Fabric'],
    ['palantir', 'Palantir Foundry'],
    ['docker', 'Docker'],
    ['postgres', 'postgres'],
    ['constructor', 'constructor'],
  ])('shows %s as %s', (platform, label) => {
    expect(platformLabel(platform)).toBe(label);
  });
});

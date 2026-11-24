import { describe, expect, it } from 'vitest';
import { APP_REQUEST_POLL_MS, appRequestPollInterval, isWorking } from './appgen-hooks';

describe('appRequestPollInterval', () => {
  it('polls every 3 s while the request is queued, drafting or accepting', () => {
    expect(APP_REQUEST_POLL_MS).toBe(3000);
    for (const status of ['queued', 'drafting', 'accepting'])
      expect(appRequestPollInterval(status, false)).toBe(3000);
  });

  it('stops on every other status and before the first response', () => {
    for (const status of ['drafted', 'failed', 'accepted', 'discarded', undefined])
      expect(appRequestPollInterval(status, false)).toBe(false);
  });

  it('stops once the time limit is reached', () => {
    expect(appRequestPollInterval('drafting', true)).toBe(false);
    expect(isWorking('drafting')).toBe(true);
    expect(isWorking('drafted')).toBe(false);
  });
});

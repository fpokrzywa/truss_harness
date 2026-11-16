import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { stubInt } from '@/test/integrate';
import { bodyOf, list } from '@/test/security';
import { jsonResponse, renderWithClient } from '@/test/utils';
import { PlatformVendorsView } from './VendorsView';

afterEach(() => vi.unstubAllGlobals());

const V = {
  id: 'v-1',
  key: 'snowflake',
  name: 'Snowflake',
  category: 'data_platform',
  status: 'active',
  is_global: true,
  row_version: 3,
};

describe('PlatformVendorsView', () => {
  it('manages the global catalog through /api/v1/platform/vendors', async () => {
    const fetchMock = stubInt({
      'GET /api/v1/platform/vendors': () => list([V]),
      'PATCH /api/v1/platform/vendors/v-1': () =>
        jsonResponse({ ...V, status: 'disabled', row_version: 4 }),
    });
    renderWithClient(<PlatformVendorsView />);
    expect(await screen.findByRole('cell', { name: 'Snowflake' })).toBeInTheDocument();
    // Global entries are editable here, unlike in the tenant catalog.
    await userEvent.click(screen.getByRole('button', { name: /Disable/ }));
    await userEvent.click(
      within(screen.getByRole('dialog')).getByRole('button', { name: 'Disable vendor' }),
    );
    await waitFor(() =>
      expect(bodyOf(fetchMock, 'PATCH /api/v1/platform/vendors/v-1')).toEqual({
        row_version: 3,
        status: 'disabled',
      }),
    );
  });
});

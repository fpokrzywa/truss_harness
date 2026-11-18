import { render, screen, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { OrganizationsTable } from './OrganizationsView';

describe('OrganizationsTable status', () => {
  it('shows each client status, a suspended one included', () => {
    render(
      <OrganizationsTable
        orgs={[
          { id: '1', slug: 'acme', name: 'Acme', status: 'active' },
          { id: '2', slug: 'globex', name: 'Globex', status: 'suspended' },
          { id: '3', slug: 'initech', name: 'Initech', status: 'archived' },
        ]}
      />,
    );
    const row = (name: string) => screen.getByRole('link', { name }).closest('tr') as HTMLElement;
    expect(within(row('Acme')).getByText('Active')).toBeInTheDocument();
    expect(within(row('Globex')).getByText('Suspended')).toBeInTheDocument();
    expect(within(row('Initech')).getByText('Archived')).toBeInTheDocument();
  });
});

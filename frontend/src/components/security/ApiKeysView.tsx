'use client';

import { useState } from 'react';
import type { ApiKeyFilters, PrincipalType } from '@/lib/api/security-types';
import { PageHeader, SelectField } from '../admin/ui';
import { ApiKeysPanel } from './ApiKeysPanel';
import { useOrgParam } from './useOrg';

export function ApiKeysView() {
  const org = useOrgParam();
  const [type, setType] = useState<'' | PrincipalType>('');
  const filters: ApiKeyFilters = type ? { principal_type: type } : {};

  return (
    <div className="space-y-6">
      <PageHeader
        title="API keys"
        description="Key metadata only: secrets are shown once at creation or rotation and can never be retrieved."
      />
      <div className="max-w-xs">
        <SelectField
          label="Filter by principal type"
          value={type}
          onChange={(v) => setType(v as '' | PrincipalType)}
          options={[
            { value: '', label: 'All keys' },
            { value: 'service_account', label: 'Service accounts' },
            { value: 'user', label: 'Users' },
          ]}
        />
      </div>
      <ApiKeysPanel org={org} filters={filters} />
    </div>
  );
}

'use client';

import { useState } from 'react';
import type { ArtifactKind } from '@/lib/api/build-types';
import type { ConnectionType } from '@/lib/api/integrate-types';
import { latestRef, parseRef } from '@/lib/agent-spec';
import { useArtifacts, useVersions } from '@/lib/build-hooks';
import { useConnections, useEffectiveModels } from '@/lib/integrate-hooks';
import { ErrorMessage } from '../ErrorMessage';
import { SelectField } from '../admin/ui';

/**
 * Picks a published version of an artifact: artifact first, then "latest published" (when allowed,
 * stored as `latest:{artifact_id}` and pinned by the server at publish) or a specific published version.
 */
export function VersionPicker({
  org,
  kind,
  label,
  value,
  onChange,
  allowLatest = false,
  disabled,
}: {
  org: string;
  kind: ArtifactKind;
  label: string;
  value: string;
  onChange: (value: string) => void;
  allowLatest?: boolean;
  disabled?: boolean;
}) {
  const parsed = value ? parseRef(value) : null;
  const [artifactId, setArtifactId] = useState(parsed?.mode === 'latest' ? parsed.artifactId : '');
  const artifacts = useArtifacts(org, kind, { status: 'active' });
  const versions = useVersions(org, kind, artifactId, !!artifactId);
  const published = (versions.data ?? []).filter((v) => v.state === 'published');
  const withLatest = artifacts.data?.items.filter((a) => a.latest_published_version) ?? [];
  const pinnedUnknown = parsed?.mode === 'pinned' && !artifactId;

  const current = parsed?.mode === 'latest' ? 'latest' : (parsed?.versionId ?? '');

  return (
    <div className="grid gap-2 sm:grid-cols-2">
      <SelectField
        label={`${label}: artifact`}
        value={artifactId}
        disabled={disabled}
        onChange={(id) => {
          setArtifactId(id);
          onChange(allowLatest && id ? latestRef(id) : '');
        }}
        options={[
          { value: '', label: pinnedUnknown ? `Pinned version ${value}` : 'Select...' },
          ...withLatest.map((a) => ({ value: a.id, label: `${a.name} (${a.key})` })),
        ]}
      />
      <SelectField
        label={`${label}: version`}
        value={current}
        disabled={disabled || !artifactId}
        onChange={(v) => onChange(v === 'latest' ? latestRef(artifactId) : v)}
        options={[
          { value: '', label: 'Select...' },
          ...(allowLatest ? [{ value: 'latest', label: 'Latest published' }] : []),
          ...published.map((v) => ({
            value: v.id,
            label: `Pinned v${v.version_no}`,
          })),
        ]}
      />
    </div>
  );
}

/** Ordered list of version references (strings) with add/remove. */
export function VersionRefList({
  org,
  kind,
  label,
  values,
  onChange,
  allowLatest,
  readOnly,
}: {
  org: string;
  kind: ArtifactKind;
  label: string;
  values: string[];
  onChange: (values: string[]) => void;
  allowLatest?: boolean;
  readOnly?: boolean;
}) {
  return (
    <div className="space-y-3">
      {values.map((v, i) => (
        <div key={i} className="rounded-md border border-slate-200 p-2">
          <VersionPicker
            org={org}
            kind={kind}
            label={`${label} ${i + 1}`}
            value={v}
            allowLatest={allowLatest}
            disabled={readOnly}
            onChange={(next) => onChange(values.map((x, j) => (j === i ? next : x)))}
          />
          {!readOnly && (
            <button
              type="button"
              className="btn mt-2"
              onClick={() => onChange(values.filter((_, j) => j !== i))}
            >
              Remove {label.toLowerCase()} {i + 1}
            </button>
          )}
        </div>
      ))}
      {!readOnly && (
        <button type="button" className="btn" onClick={() => onChange([...values, ''])}>
          Add {label.toLowerCase()}
        </button>
      )}
    </div>
  );
}

/**
 * Effective (allowed, usable) models for the organization, or for a workspace and project when given.
 * Disabled, with a note, while loading, after a failed request and when no model is available.
 */
export function ModelSelect({
  org,
  workspace,
  project,
  label,
  value,
  onChange,
  emptyLabel = 'Select a model...',
  disabled,
}: {
  org: string;
  workspace?: string;
  project?: string;
  label: string;
  value: string;
  onChange: (id: string) => void;
  emptyLabel?: string;
  disabled?: boolean;
}) {
  const models = useEffectiveModels(org, workspace, project);
  const items = models.data?.items ?? [];
  const known = items.some((m) => m.id === value);
  const none = models.isSuccess && items.length === 0;
  return (
    <div className="space-y-1">
      <SelectField
        label={label}
        value={value}
        disabled={disabled || models.isPending || models.isError || none}
        onChange={onChange}
        options={[
          { value: '', label: emptyLabel },
          ...(value && !known ? [{ value, label: value }] : []),
          ...items.map((m) => ({ value: m.id, label: `${m.display_name} (${m.model_key})` })),
        ]}
      />
      {models.isPending && (
        <p role="status" className="text-sm text-slate-600">
          Loading models...
        </p>
      )}
      {models.isError && <ErrorMessage error={models.error} title="Could not load models" />}
      {none && (
        <p className="text-sm text-slate-700">
          No models are available here. An administrator can allow models on the Models screen.
        </p>
      )}
    </div>
  );
}

/** Active connections of the given types. */
export function ConnectionSelect({
  org,
  label,
  types,
  value,
  onChange,
  disabled,
}: {
  org: string;
  label: string;
  types: ConnectionType[];
  value: string;
  onChange: (id: string) => void;
  disabled?: boolean;
}) {
  const conns = useConnections(org);
  const items = (conns.data?.items ?? []).filter(
    (c) => types.includes(c.connection_type) && c.status === 'active',
  );
  const known = items.some((c) => c.id === value);
  return (
    <SelectField
      label={label}
      value={value}
      disabled={disabled}
      onChange={onChange}
      options={[
        { value: '', label: 'Select a connection...' },
        ...(value && !known ? [{ value, label: value }] : []),
        ...items.map((c) => ({ value: c.id, label: `${c.name} (${c.connection_type})` })),
      ]}
    />
  );
}

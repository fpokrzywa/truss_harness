'use client';

import Link from 'next/link';
import { useState } from 'react';
import type { Artifact } from '@/lib/api/build-types';
import { useCan, useProjects, useSession, useWorkspaces } from '@/lib/hooks';
import { projHref } from '@/lib/scope';
import { SelectField } from '../admin/ui';

/**
 * "Run" entry on the agent detail page (agent:execute). A run needs a project, so the user picks
 * workspace and project first; the console opens with this agent preselected. Hidden until the
 * backend advertises the `operate` feature (D-013).
 */
export function RunAgentLink({ org, artifact }: { org: string; artifact: Artifact }) {
  const { can } = useCan(org);
  const session = useSession();
  const [ws, setWs] = useState('');
  const [proj, setProj] = useState('');
  const workspaces = useWorkspaces(org);
  const projects = useProjects(org, ws || undefined);
  if (!session.data?.features?.includes('operate') || !can('agent:execute')) return null;
  if (artifact.status !== 'active' || !artifact.latest_published_version) {
    return (
      <p role="note" className="text-sm text-slate-600">
        Publish and activate this agent to run it.
      </p>
    );
  }
  return (
    <section aria-label="Run this agent" className="card flex flex-wrap items-end gap-3">
      <SelectField
        label="Run in workspace"
        value={ws}
        onChange={(v) => {
          setWs(v);
          setProj('');
        }}
        options={[
          { value: '', label: 'Select...' },
          ...(workspaces.data?.items ?? []).map((w) => ({ value: w.slug, label: w.name })),
        ]}
      />
      <SelectField
        label="Run in project"
        value={proj}
        disabled={!ws}
        onChange={setProj}
        options={[
          { value: '', label: 'Select...' },
          ...(projects.data?.items ?? []).map((p) => ({ value: p.slug, label: p.name })),
        ]}
      />
      {ws && proj ? (
        <Link
          className="btn btn-primary"
          href={`${projHref(org, ws, proj)}/run?agent=${encodeURIComponent(artifact.id)}`}
        >
          Run
        </Link>
      ) : (
        <button type="button" className="btn btn-primary" disabled>
          Run
        </button>
      )}
    </section>
  );
}

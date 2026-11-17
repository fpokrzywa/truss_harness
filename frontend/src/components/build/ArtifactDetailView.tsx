'use client';

import Link from 'next/link';
import { useState } from 'react';
import { endpoints } from '@/lib/api/endpoints';
import type { Artifact, ArtifactKind, ArtifactStatus } from '@/lib/api/build-types';
import { agentPayload } from './AgentEditor';
import { useArtifact, useRefreshBuild } from '@/lib/build-hooks';
import { useCan } from '@/lib/hooks';
import { buildHref } from '@/lib/scope';
import { ActionError, Badge, PageHeader, QueryGate, useAction } from '../admin/ui';
import { ConfirmDialog } from '../ConfirmDialog';
import { useOrgParam } from '../security/useOrg';
import { AgentEditor, agentBlockers, normalizeAgentSpec } from './AgentEditor';
import { AgentTestPanel } from './AgentTestPanel';
import { DraftEditor, type TestPanelProps } from './DraftEditor';
import {
  OrchestrationEditor,
  normalizeOrchestrationSpec,
  orchestrationBlockers,
} from './OrchestrationEditor';
import { RunAgentLink } from '../operate/RunAgentLink';
import { PromptEditor, normalizePromptSpec } from './PromptEditor';
import { PromptPreviewPanel, PromptTestPanel } from './PromptPanels';
import { SkillEditor, normalizeSkillSpec, skillBlockers } from './SkillEditor';
import { ToolEditor, normalizeToolSpec, toolBlockers } from './ToolEditor';
import { ToolTestPanel } from './ToolTestPanel';
import {
  PipelineEditor,
  normalizePipelineSpec,
  pipelineBlockers,
} from '../pipelines/PipelineEditor';
import { PipelineHistory } from '../pipelines/PipelineHistory';
import { PipelineRunProvider } from '../pipelines/PipelineRunContext';
import { PipelineRunPanel, PipelineTestPanel } from '../pipelines/PipelineRunPanels';
import { PipelineValidationPanel } from '../pipelines/PipelineValidationPanel';
import { SchedulesPanel } from '../pipelines/SchedulesPanel';
import { ActionsPanel } from '../ontology/ActionsPanel';
import { ExplorerPanel } from '../ontology/ExplorerPanel';
import { LineagePanel } from '../ontology/LineagePanel';
import {
  OntologyEditor,
  normalizeOntologySpec,
  ontologyBlockers,
} from '../ontology/OntologyEditor';
import { OntologyValidationPanel } from '../ontology/OntologyValidationPanel';
import { PoliciesPanel } from '../ontology/PoliciesPanel';
import { DiffPanel, VersionsPanel } from './VersionsPanel';
import { KIND_INFO, latestLabel, ScopeLabel } from './shared';

function PromptPanels(p: TestPanelProps<ReturnType<typeof normalizePromptSpec>>) {
  return (
    <div className="space-y-4">
      <PromptPreviewPanel {...p} />
      <PromptTestPanel {...p} />
    </div>
  );
}

type Tab =
  | 'editor'
  | 'versions'
  | 'compare'
  | 'run'
  | 'history'
  | 'schedules'
  | 'policies'
  | 'explorer'
  | 'lineage'
  | 'actions';

export function ArtifactDetailView({
  kind,
  id,
  initialTab,
  lineageHighlight,
}: {
  kind: ArtifactKind;
  id: string;
  initialTab?: Tab;
  /** Ontology lineage record id to highlight in the Lineage tab. */
  lineageHighlight?: string;
}) {
  const org = useOrgParam();
  const artifact = useArtifact(org, kind, id);
  const info = KIND_INFO[kind];
  return (
    <div className="space-y-6">
      <p className="text-sm">
        <Link className="text-blue-800 underline" href={buildHref(org, kind)}>
          {info.label}
        </Link>
      </p>
      <QueryGate query={artifact} noun={info.singular}>
        {(a) => (
          <Detail
            org={org}
            kind={kind}
            artifact={a}
            initialTab={initialTab}
            lineageHighlight={lineageHighlight}
          />
        )}
      </QueryGate>
    </div>
  );
}

function Detail({
  org,
  kind,
  artifact,
  initialTab,
  lineageHighlight,
}: {
  org: string;
  kind: ArtifactKind;
  artifact: Artifact;
  initialTab?: Tab;
  lineageHighlight?: string;
}) {
  const info = KIND_INFO[kind];
  const { can } = useCan(org);
  const [tab, setTab] = useState<Tab>(initialTab ?? 'editor');
  const canUpdate = can(`${info.perm}:update`);
  const canPublish = can(`${info.perm}:publish`);
  const tabs: { id: Tab; label: string }[] = [
    { id: 'editor', label: 'Editor' },
    { id: 'versions', label: 'Versions' },
    { id: 'compare', label: 'Compare' },
    ...(kind === 'pipelines'
      ? [
          { id: 'run' as const, label: 'Run' },
          { id: 'history' as const, label: 'History' },
          { id: 'schedules' as const, label: 'Schedules' },
        ]
      : []),
    ...(kind === 'ontologies'
      ? [
          ...(can('ontology:manage_policies')
            ? [{ id: 'policies' as const, label: 'Policies' }]
            : []),
          ...(can('ontology:query')
            ? [
                { id: 'explorer' as const, label: 'Explorer' },
                { id: 'actions' as const, label: 'Actions' },
              ]
            : []),
          { id: 'lineage' as const, label: 'Lineage' },
        ]
      : []),
  ];

  return (
    <>
      <PageHeader
        title={artifact.name}
        description={artifact.description ?? undefined}
        actions={<StatusActions org={org} kind={kind} artifact={artifact} can={can} />}
      />
      {kind === 'agents' && <RunAgentLink org={org} artifact={artifact} />}
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <code className="rounded bg-slate-100 px-1 font-mono text-xs">{artifact.key}</code>
        <Badge tone={artifact.status === 'active' ? 'good' : 'neutral'}>{artifact.status}</Badge>
        <Badge tone={artifact.latest_published_version ? 'good' : 'neutral'}>
          Latest published: {latestLabel(artifact)}
        </Badge>
        {artifact.has_draft && <Badge tone="warn">Draft in progress</Badge>}
        <span className="text-slate-600">
          <ScopeLabel org={org} scope={artifact.scope} /> · {artifact.visibility}
        </span>
      </div>

      <div
        role="tablist"
        aria-label={`${info.singular} sections`}
        className="flex gap-1 border-b border-slate-200"
      >
        {tabs.map((t) => (
          <button
            key={t.id}
            type="button"
            role="tab"
            id={`tab-${t.id}`}
            aria-selected={tab === t.id}
            aria-controls={`panel-${t.id}`}
            className={`px-3 py-2 text-sm font-medium ${
              tab === t.id ? 'border-b-2 border-blue-800 text-blue-900' : 'text-slate-600'
            }`}
            onClick={() => setTab(t.id)}
          >
            {t.label}
          </button>
        ))}
      </div>
      <PipelineRunProvider key={tab}>
        <div role="tabpanel" id={`panel-${tab}`} aria-labelledby={`tab-${tab}`}>
          {tab === 'editor' && (
            <KindEditor
              org={org}
              kind={kind}
              artifact={artifact}
              canUpdate={canUpdate}
              canPublish={canPublish}
            />
          )}
          {tab === 'versions' && (
            <VersionsPanel
              org={org}
              kind={kind}
              artifact={artifact}
              canUpdate={canUpdate}
              canPublish={canPublish}
            />
          )}
          {tab === 'compare' && <DiffPanel org={org} kind={kind} artifact={artifact} />}
          {tab === 'run' && <PipelineRunPanel org={org} artifact={artifact} />}
          {tab === 'history' && <PipelineHistory org={org} pipelineId={artifact.id} />}
          {tab === 'schedules' && <SchedulesPanel org={org} pipelineId={artifact.id} />}
          {tab === 'policies' && <PoliciesPanel org={org} artifact={artifact} />}
          {tab === 'explorer' && <ExplorerPanel org={org} artifact={artifact} />}
          {tab === 'actions' && (
            <ActionsPanel org={org} artifact={artifact} canInvoke={can('ontology:query')} />
          )}
          {tab === 'lineage' && (
            <LineagePanel org={org} artifact={artifact} highlight={lineageHighlight} />
          )}
        </div>
      </PipelineRunProvider>
    </>
  );
}

function KindEditor(props: {
  org: string;
  kind: ArtifactKind;
  artifact: Artifact;
  canUpdate: boolean;
  canPublish: boolean;
}) {
  const common = {
    org: props.org,
    kind: props.kind,
    artifact: props.artifact,
    canUpdate: props.canUpdate,
    canPublish: props.canPublish,
  };
  switch (props.kind) {
    case 'prompts':
      return (
        <DraftEditor
          {...common}
          normalize={normalizePromptSpec}
          Editor={PromptEditor}
          TestPanel={PromptPanels}
        />
      );
    case 'tools':
      return (
        <DraftEditor
          {...common}
          normalize={normalizeToolSpec}
          Editor={ToolEditor}
          TestPanel={ToolTestPanel}
          blockers={toolBlockers}
        />
      );
    case 'skills':
      return (
        <DraftEditor
          {...common}
          normalize={normalizeSkillSpec}
          Editor={SkillEditor}
          blockers={skillBlockers}
        />
      );
    case 'orchestrations':
      return (
        <DraftEditor
          {...common}
          normalize={normalizeOrchestrationSpec}
          Editor={OrchestrationEditor}
          blockers={orchestrationBlockers}
        />
      );
    case 'pipelines':
      return (
        <DraftEditor
          {...common}
          normalize={normalizePipelineSpec}
          Editor={PipelineEditor}
          TestPanel={PipelineTestPanel}
          ValidationView={PipelineValidationPanel}
          blockers={pipelineBlockers}
        />
      );
    case 'ontologies':
      return (
        <DraftEditor
          {...common}
          normalize={normalizeOntologySpec}
          Editor={OntologyEditor}
          ValidationView={OntologyValidationPanel}
          blockers={ontologyBlockers}
        />
      );
    case 'agents':
      return (
        <DraftEditor
          {...common}
          normalize={normalizeAgentSpec}
          Editor={AgentEditor}
          TestPanel={AgentTestPanel}
          blockers={agentBlockers}
          toPayload={agentPayload}
        />
      );
  }
}

/** Status transitions. Agents use the dedicated activate endpoint (agent:activate) for active/disabled. */
function StatusActions({
  org,
  kind,
  artifact,
  can,
}: {
  org: string;
  kind: ArtifactKind;
  artifact: Artifact;
  can: (perm: string) => boolean;
}) {
  const info = KIND_INFO[kind];
  const refresh = useRefreshBuild(org);
  const { busy, error, run } = useAction();
  const [retiring, setRetiring] = useState(false);
  const canUpdate = can(`${info.perm}:update`);
  const canToggle = kind === 'agents' ? can('agent:activate') : canUpdate;

  async function change(status: ArtifactStatus) {
    const r = await run(() =>
      kind === 'agents' && status !== 'retired'
        ? endpoints.setAgentStatus(org, artifact.id, status)
        : endpoints.updateArtifact(org, kind, artifact.id, {
            status,
            row_version: artifact.row_version,
          }),
    );
    if (r.ok) {
      setRetiring(false);
      await refresh();
    }
  }

  if (artifact.status === 'retired') return null;
  const active = artifact.status === 'active';
  return (
    <div className="flex flex-col items-end gap-2">
      <div className="flex gap-2">
        {canToggle && (
          <button
            type="button"
            className="btn"
            disabled={busy}
            onClick={() => void change(active ? 'disabled' : 'active')}
          >
            {kind === 'agents'
              ? active
                ? 'Deactivate'
                : 'Activate'
              : active
                ? 'Disable'
                : 'Enable'}
          </button>
        )}
        {canUpdate && (
          <button
            type="button"
            className="btn btn-danger"
            disabled={busy}
            onClick={() => setRetiring(true)}
          >
            Retire {info.singular}
          </button>
        )}
      </div>
      {!retiring && <ActionError error={error} title="Could not change status" />}
      <ConfirmDialog
        open={retiring}
        title={`Retire this ${info.singular}?`}
        confirmLabel="Retire"
        busy={busy}
        onConfirm={() => void change('retired')}
        onCancel={() => setRetiring(false)}
      >
        <p>
          Retired {info.label.toLowerCase()} can no longer be used or referenced by new versions.
          This cannot be undone.
        </p>
        <ActionError error={error} title="Could not retire" />
      </ConfirmDialog>
    </div>
  );
}

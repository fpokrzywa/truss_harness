'use client';

import { useCallback, useState, type ComponentType } from 'react';
import { ApiError } from '@/lib/api/errors';
import { endpoints } from '@/lib/api/endpoints';
import type {
  Artifact,
  ArtifactKind,
  ArtifactVersion,
  PublishResult,
  ValidationResult,
} from '@/lib/api/build-types';
import { useDraft, useRefreshBuild, useVersion } from '@/lib/build-hooks';
import { ActionError, Badge, Modal, TextField, useAction } from '../admin/ui';
import { QueryError } from '../QueryError';
import { SectionTabs } from './SectionTabs';
import { Section } from './shared';

export interface EditorProps<S> {
  org: string;
  artifact: Artifact;
  spec: S;
  onChange: (spec: S) => void;
  readOnly: boolean;
  /** Reports (message) or clears (null) an unparseable field; any report blocks save and publish. */
  reportInvalid: (field: string, message: string | null) => void;
  /** Latest server validation of the saved draft (pipelines highlight erroring nodes/edges from it). */
  validation?: ValidationResult | null;
  /** True when the working copy differs from the saved draft. */
  dirty?: boolean;
  /** True when a saved draft exists server-side (ontology mappings attach to it). */
  hasDraft?: boolean;
}

export interface TestPanelProps<S> {
  org: string;
  artifact: Artifact;
  spec: S;
  /** True when the working copy differs from the saved draft (tests run on the saved draft). */
  dirty: boolean;
}

interface DraftEditorProps<S> {
  org: string;
  kind: ArtifactKind;
  artifact: Artifact;
  normalize: (raw: Record<string, unknown> | undefined) => S;
  Editor: ComponentType<EditorProps<S>>;
  TestPanel?: ComponentType<TestPanelProps<S>>;
  /** Replaces the generic validation results panel (e.g. pipelines list errors by code with topo order). */
  ValidationView?: ComponentType<{ result: ValidationResult }>;
  /** Client-side blockers mirroring server rules; they disable save and publish. */
  blockers?: (spec: S) => string[];
  /** Final shaping of the saved spec (e.g. invariants the UI must always send). */
  toPayload?: (spec: S) => S;
  canUpdate: boolean;
  canPublish: boolean;
}

/** Loads the draft (or the latest published spec when none exists) and mounts the working copy. */
export function DraftEditor<S>(props: DraftEditorProps<S>) {
  const { org, kind, artifact } = props;
  const draft = useDraft(org, kind, artifact.id);
  const latestId = draft.data === null ? artifact.latest_published_version?.id : undefined;
  const latest = useVersion(org, kind, artifact.id, latestId);
  const [epoch, setEpoch] = useState(0);

  if (draft.isPending) return <p role="status">Loading draft...</p>;
  if (draft.isError) return <QueryError error={draft.error} />;
  if (latestId && latest.isPending) return <p role="status">Loading latest version...</p>;
  if (latest.isError) return <QueryError error={latest.error} />;

  return (
    <Working
      key={epoch}
      {...props}
      draft={draft.data}
      base={latest.data}
      onReload={async () => {
        await draft.refetch();
        setEpoch((n) => n + 1);
      }}
    />
  );
}

function Working<S>({
  org,
  kind,
  artifact,
  normalize,
  Editor,
  TestPanel,
  ValidationView = ValidatePanel,
  blockers,
  toPayload,
  canUpdate,
  canPublish,
  draft,
  base,
  onReload,
}: DraftEditorProps<S> & {
  draft: ArtifactVersion | null | undefined;
  base: ArtifactVersion | undefined;
  onReload: () => Promise<void>;
}) {
  const refresh = useRefreshBuild(org);
  const source = draft?.spec ?? base?.spec;
  const [spec, setSpec] = useState<S>(() => normalize(source));
  const [rowVersion, setRowVersion] = useState<number>(draft?.row_version ?? 0);
  const [saved, setSaved] = useState<string>(() => JSON.stringify(normalize(source)));
  const [hasDraft, setHasDraft] = useState(!!draft);
  const [validation, setValidation] = useState<ValidationResult | null>(null);
  const [publishing, setPublishing] = useState(false);
  const [pending, setPending] = useState(false);
  const [invalid, setInvalid] = useState<Record<string, string>>({});
  const save = useAction();
  const validate = useAction();

  const dirty = JSON.stringify(spec) !== saved;
  const block = [...(blockers?.(spec) ?? []), ...Object.values(invalid)];
  const reportInvalid = useCallback((field: string, message: string | null) => {
    setInvalid((cur) => {
      if ((cur[field] ?? null) === message) return cur;
      const next = { ...cur };
      if (message === null) delete next[field];
      else next[field] = message;
      return next;
    });
  }, []);

  async function doSave(): Promise<boolean> {
    const r = await save.run(() =>
      endpoints.saveDraft(
        org,
        kind,
        artifact.id,
        (toPayload ? toPayload(spec) : spec) as Record<string, unknown>,
        rowVersion,
      ),
    );
    if (r.ok) {
      setRowVersion(r.value.row_version ?? rowVersion + 1);
      setSaved(JSON.stringify(spec));
      setHasDraft(true);
      setValidation(null);
      void refresh(); // fire-and-forget cache refresh; the working copy is already current
      return true;
    }
    return false;
  }

  // 409 = stale row_version: someone else saved the draft first.
  const isConflict = save.error instanceof ApiError && save.error.status === 409;

  async function doValidate() {
    if (dirty && !(await doSave())) return;
    const r = await validate.run(() => endpoints.validateDraft(org, kind, artifact.id));
    if (r.ok) setValidation(r.value);
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        {hasDraft ? (
          <Badge tone="warn">Draft</Badge>
        ) : (
          <Badge tone="neutral">
            No draft{base ? `: editing creates one from v${base.version_no}` : ''}
          </Badge>
        )}
        {dirty && <Badge tone="warn">Unsaved changes</Badge>}
        {!canUpdate && <Badge tone="neutral">Read only</Badge>}
      </div>

      {isConflict && (
        <div role="alert" className="rounded-md border border-amber-300 bg-amber-50 p-3 text-sm">
          <p className="font-medium text-amber-900">
            This draft was changed by someone else since you opened it.
          </p>
          <p className="mt-1 text-amber-900">
            Your edits were not saved. Reload to get the latest draft (your unsaved edits will be
            discarded).
          </p>
          <button type="button" className="btn mt-2" onClick={() => void onReload()}>
            Reload latest draft
          </button>
        </div>
      )}
      {!isConflict && <ActionError error={save.error} title="Could not save draft" />}

      {block.length > 0 && (
        <ul
          role="alert"
          className="list-disc rounded-md border border-red-300 bg-red-50 p-3 pl-7 text-sm text-red-800"
        >
          {block.map((b) => (
            <li key={b}>{b}</li>
          ))}
        </ul>
      )}

      {canUpdate && (
        <div className="flex flex-wrap gap-2">
          <button
            type="button"
            className="btn btn-primary"
            disabled={!dirty || save.busy || block.length > 0}
            onClick={() => void doSave()}
          >
            {save.busy ? 'Saving...' : 'Save draft'}
          </button>
          <button
            type="button"
            className="btn"
            disabled={validate.busy || save.busy || block.length > 0 || (!hasDraft && !dirty)}
            onClick={() => void doValidate()}
          >
            {validate.busy ? 'Validating...' : 'Validate'}
          </button>
          {canPublish && (
            <button
              type="button"
              className="btn"
              disabled={!hasDraft || dirty || block.length > 0}
              title={dirty ? 'Save the draft before publishing' : undefined}
              onClick={() => setPublishing(true)}
            >
              Publish
            </button>
          )}
        </div>
      )}
      {pending && (
        <p role="status" className="text-sm text-amber-900">
          Publish request pending approval. An approver must approve it before this version is
          published.
        </p>
      )}
      <ActionError error={validate.error} title="Validation failed to run" />
      {validation && <ValidationView result={validation} />}

      {/* Save, Validate and Publish sit above the tabs so they are reachable from every section. */}
      <SectionTabs label="Editor sections">
        <div className="space-y-4">
          <Editor
            org={org}
            artifact={artifact}
            spec={spec}
            onChange={setSpec}
            readOnly={!canUpdate}
            reportInvalid={reportInvalid}
            validation={validation}
            dirty={dirty}
            hasDraft={hasDraft}
          />
          {TestPanel && (
            <Section title="Test" plain>
              <TestPanel org={org} artifact={artifact} spec={spec} dirty={dirty || !hasDraft} />
            </Section>
          )}
        </div>
      </SectionTabs>

      {publishing && (
        <PublishDialog
          org={org}
          kind={kind}
          artifactId={artifact.id}
          onClose={() => setPublishing(false)}
          onResult={(r) => {
            if (r.kind === 'pending') setPending(true);
            else {
              setHasDraft(false);
              setPending(false);
            }
            void refresh();
          }}
        />
      )}
    </div>
  );
}

export function ValidatePanel({ result }: { result: ValidationResult }) {
  return (
    <section aria-label="Validation results" className="card space-y-2">
      <h3 className="text-sm font-semibold">
        {result.valid ? 'Draft is valid' : `Draft has ${result.errors.length} error(s)`}
      </h3>
      {result.errors.length > 0 && (
        <ul className="space-y-1 text-sm">
          {result.errors.map((e, i) => (
            <li key={`${e.path}-${i}`} className="text-red-800">
              <code className="rounded bg-red-50 px-1 font-mono text-xs">{e.path || '(root)'}</code>{' '}
              {e.message}
              {e.code && <span className="ml-1 text-xs text-slate-500">({e.code})</span>}
            </li>
          ))}
        </ul>
      )}
      {(result.warnings?.length ?? 0) > 0 && (
        <ul className="space-y-1 text-sm">
          {result.warnings?.map((w, i) => (
            <li key={`${w.path}-${i}`} className="text-amber-900">
              <code className="rounded bg-amber-50 px-1 font-mono text-xs">
                {w.path || '(root)'}
              </code>{' '}
              {w.message}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

export function PublishDialog({
  org,
  kind,
  artifactId,
  onClose,
  onResult,
}: {
  org: string;
  kind: ArtifactKind;
  artifactId: string;
  onClose: () => void;
  onResult: (r: PublishResult) => void;
}) {
  const [note, setNote] = useState('');
  const [result, setResult] = useState<PublishResult | null>(null);
  const { busy, error, run } = useAction();

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    const r = await run(() => endpoints.publish(org, kind, artifactId, note.trim()));
    if (r.ok) {
      setResult(r.value);
      onResult(r.value);
    }
  }

  return (
    <Modal title="Publish version" onClose={onClose} busy={busy}>
      {result ? (
        <div className="space-y-3">
          {result.kind === 'pending' ? (
            <p role="status" className="text-sm">
              Publish request pending. Your organization requires approval before publishing; an
              approver will review it in Publish Requests.
            </p>
          ) : (
            <p role="status" className="text-sm">
              Published version {result.version.version_no}. It is now immutable.
            </p>
          )}
          <div className="flex justify-end">
            <button type="button" className="btn btn-primary" onClick={onClose}>
              Close
            </button>
          </div>
        </div>
      ) : (
        <form onSubmit={submit} className="space-y-3">
          <p className="text-sm text-slate-700">
            Publishing validates the draft, pins its references and makes the version immutable.
          </p>
          <TextField label="Change note" value={note} onChange={setNote} multiline required />
          <ActionError error={error} title="Could not publish" />
          <div className="mt-5 flex justify-end gap-2">
            <button type="button" className="btn" onClick={onClose} disabled={busy}>
              Cancel
            </button>
            <button type="submit" className="btn btn-primary" disabled={busy || !note.trim()}>
              {busy ? 'Publishing...' : 'Publish'}
            </button>
          </div>
        </form>
      )}
    </Modal>
  );
}

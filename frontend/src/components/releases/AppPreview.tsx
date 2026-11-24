'use client';

import { parseScreens, ScreenRenderer, type AppApi, type ToolResult } from '@shifde/appgen-web';
import type { AppPreviewOut, AppPreviewTool } from '@/lib/api/appgen-types';
import { ApiError } from '@/lib/api/errors';
import { useAppPreview } from '@/lib/release-hooks';
import { EmptyState, QueryGate } from '../admin/ui';

/** Plain words for the 409s of the preview route; any other 409 shows its problem detail. */
const CONFLICTS: Record<string, string> = {
  release_has_no_app: 'This release has no generated app.',
  release_source_changed:
    'A version this release pins has changed or cannot be read; seal the release again.',
};

const SAMPLE_ROWS = 3;

/** The sample result of `tool`: one column per parameter (or two generic ones), fixed rows. */
function sampleResult(tool: AppPreviewTool | undefined): ToolResult {
  const names = tool?.parameters.length
    ? tool.parameters.map((p) => p.name)
    : ['column_1', 'column_2'];
  return {
    columns: names.map((name) => ({ name, type: 'string' })),
    rows: Array.from({ length: SAMPLE_ROWS }, (_, row) =>
      names.map((_, col) => `sample ${row + 1}.${col + 1}`),
    ),
    row_count: SAMPLE_ROWS,
    truncated: false,
  };
}

/** A static, in-memory `AppApi`: fixed answers and sample rows, never a network call. */
export function previewApi(tools: AppPreviewTool[]): AppApi {
  return {
    ask: () =>
      Promise.resolve({
        answer: 'This is a preview: the deployed app answers here.',
        tool_calls: [],
      }),
    runTool: (tool) => Promise.resolve(sampleResult(tools.find((t) => t.name === tool))),
  };
}

/** The app a `/2` release generates: its files (metadata only) and its screens over sample data. */
export function AppPreview({ org, releaseId }: { org: string; releaseId: string }) {
  const preview = useAppPreview(org, releaseId, true);
  const conflict =
    preview.error instanceof ApiError && preview.error.status === 409 ? preview.error : null;
  return (
    <section aria-label="App preview" className="card space-y-4">
      <h2 className="text-base font-semibold">App preview</h2>
      {conflict ? (
        <p role="alert" className="text-sm text-red-800">
          {CONFLICTS[conflict.code] ?? conflict.detail ?? conflict.message}
        </p>
      ) : (
        <QueryGate query={preview} noun="app preview">
          {(p) => <PreviewBody preview={p} />}
        </QueryGate>
      )}
    </section>
  );
}

function PreviewBody({ preview }: { preview: AppPreviewOut }) {
  const screens = parseScreens(preview.screens);
  return (
    <>
      <dl className="grid gap-x-4 gap-y-1 text-sm sm:grid-cols-[12rem_1fr]">
        <dt className="text-slate-600">Generator version</dt>
        <dd className="font-mono text-xs">{preview.generator_version}</dd>
      </dl>
      <table className="w-full">
        <caption className="text-left text-sm font-semibold">Files</caption>
        <thead className="border-b border-slate-200">
          <tr>
            {['Path', 'Size (bytes)', 'SHA-256'].map((h) => (
              <th key={h} scope="col" className="th">
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-100">
          {preview.files.map((f) => (
            <tr key={f.path}>
              <td className="td font-mono text-xs break-all">{f.path}</td>
              <td className="td font-mono text-xs">{f.size}</td>
              <td className="td font-mono text-xs break-all">{f.sha256}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <h3 className="text-sm font-semibold">Screens</h3>
      {screens === null ? (
        <EmptyState>The screens of this release cannot be shown.</EmptyState>
      ) : (
        <>
          <p className="text-sm text-slate-600">
            The rows below are sample data; nothing is sent to the app, a model or a data platform.
          </p>
          <div className="space-y-3 rounded border border-slate-200 p-3 text-sm [&_button]:rounded [&_button]:border [&_button]:border-slate-300 [&_button]:px-2 [&_button]:py-1 [&_button[aria-current=page]]:bg-slate-100 [&_form]:flex [&_form]:flex-wrap [&_form]:items-end [&_form]:gap-2 [&_h2]:font-semibold [&_input]:rounded [&_input]:border [&_input]:border-slate-300 [&_input]:px-2 [&_input]:py-1 [&_label]:flex [&_label]:flex-col [&_nav]:flex [&_nav]:flex-wrap [&_nav]:gap-2 [&_section]:space-y-2 [&_td]:border-t [&_td]:border-slate-100 [&_td]:px-2 [&_td]:py-1 [&_th]:px-2 [&_th]:py-1 [&_th]:text-left">
            <ScreenRenderer screens={screens} api={previewApi(preview.tools)} />
          </div>
        </>
      )}
    </>
  );
}

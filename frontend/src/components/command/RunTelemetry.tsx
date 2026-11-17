import Link from 'next/link';
import type { HealthDetails } from '@/lib/api/governance-types';
import type { Run } from '@/lib/api/operate-types';
import { TONE_LABEL, runDuration, runLabel, runTone, tallyRuns, type Tone } from '@/lib/command';
import { orgHref } from '@/lib/scope';
import { Reveal } from './Reveal';
import { ToneGlyph } from './ToneGlyph';

const TONES: readonly Tone[] = ['good', 'active', 'warn', 'idle'];
const TABLE_ROWS = 4;

const runHref = (org: string, id: string) => `${orgHref(org)}/runs/${encodeURIComponent(id)}`;
const when = (at: string) => new Date(at).toLocaleString();

function StatCard({
  index,
  label,
  value,
  success = false,
}: {
  index: number;
  label: string;
  value: number;
  /** Green is reserved for success, so other counts stay white. */
  success?: boolean;
}) {
  return (
    <Reveal index={index}>
      <div className="h-full border border-shi-blue-slate bg-shi-grey px-3 py-2">
        <dt className="text-sm font-medium">{label}</dt>
        <dd className={`font-roboto-mono text-2xl ${success ? 'text-shi-green' : ''}`}>{value}</dd>
      </div>
    </Reveal>
  );
}

/** Newest first, one 44px target per run. Each opens that run; shape and tooltip name its state. */
function RunStrip({ org, runs }: { org: string; runs: readonly Run[] }) {
  return (
    <ol aria-label="Recent runs, newest first" className="relative flex flex-wrap gap-0.5">
      {runs.map((run) => (
        <li key={run.id}>
          <Link
            href={runHref(org, run.id)}
            aria-label={`${runLabel(run)}: ${run.workspace.slug}/${run.project.slug}, ${when(run.created_at)}`}
            className="cc-run flex h-11 w-11 items-center justify-center bg-shi-grey hover:bg-shi-mid"
          >
            <ToneGlyph tone={runTone(run)} size={16} />
            <span
              aria-hidden="true"
              className="cc-tip absolute bottom-full left-0 z-10 mb-1 w-max max-w-72 border border-shi-light bg-shi-grey px-3 py-2 text-sm"
            >
              <span className="block font-medium">{runLabel(run)}</span>
              <span className="block text-shi-tint">
                {run.workspace.slug}/{run.project.slug}
              </span>
              <span className="block text-shi-tint">{when(run.created_at)}</span>
            </span>
          </Link>
        </li>
      ))}
    </ol>
  );
}

export function RunTelemetry({
  org,
  runs,
  runsUnavailable,
  details,
}: {
  org: string;
  runs: readonly Run[] | undefined;
  /** Set when the run list cannot be shown; says why. */
  runsUnavailable?: string;
  details?: HealthDetails;
}) {
  const tally = runs ? tallyRuns(runs) : null;
  return (
    <div className="space-y-4">
      {runsUnavailable && <p className="max-w-[65ch] text-sm">{runsUnavailable}</p>}

      <dl className="grid grid-cols-2 gap-2 md:grid-cols-4">
        {tally &&
          TONES.map((tone, i) => (
            <StatCard
              key={tone}
              index={i}
              label={TONE_LABEL[tone]}
              value={tally[tone]}
              success={tone === 'good'}
            />
          ))}
        {details && (
          <>
            <StatCard index={0} label="Jobs queued" value={details.queue.queued} />
            <StatCard index={1} label="Jobs leased" value={details.queue.leased} />
            <StatCard index={2} label="Dead jobs" value={details.queue.dead_in_window} />
            <StatCard index={3} label="Live workers" value={details.workers.running} />
          </>
        )}
      </dl>

      {runs && runs.length === 0 && (
        <p className="max-w-[65ch] text-sm">
          No runs in this organization yet. Runs appear here as soon as an agent or pipeline starts.
        </p>
      )}

      {runs && runs.length > 0 && (
        <Reveal className="space-y-4">
          <div>
            <h3 className="mb-2 text-sm font-medium">Last {runs.length} runs, newest first</h3>
            <RunStrip org={org} runs={runs} />
            <ul className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-sm text-shi-tint">
              {TONES.map((tone) => (
                <li key={tone} className="flex items-center gap-2">
                  <ToneGlyph tone={tone} size={12} />
                  {TONE_LABEL[tone]}
                </li>
              ))}
            </ul>
          </div>

          <div
            role="region"
            aria-label="Most recent runs"
            tabIndex={0}
            className="overflow-x-auto border border-shi-blue-slate bg-shi-grey"
          >
            <table className="w-full min-w-[36rem] border-collapse font-roboto-mono text-sm">
              <thead>
                <tr className="border-b border-shi-light text-left font-roboto">
                  <th className="px-3 py-1.5 font-medium">State</th>
                  <th className="px-3 py-1.5 font-medium">Kind</th>
                  <th className="px-3 py-1.5 font-medium">Project</th>
                  <th className="px-3 py-1.5 font-medium">Created</th>
                  <th className="px-3 py-1.5 font-medium">Duration</th>
                </tr>
              </thead>
              <tbody>
                {runs.slice(0, TABLE_ROWS).map((run) => (
                  <tr key={run.id} className="border-b border-shi-blue-slate last:border-b-0">
                    <td className="px-3 py-1.5">
                      <Link
                        href={runHref(org, run.id)}
                        className="inline-flex min-h-6 items-center gap-2 underline underline-offset-4"
                      >
                        <ToneGlyph tone={runTone(run)} size={12} />
                        {runLabel(run)}
                      </Link>
                    </td>
                    <td className="px-3 py-1.5">{run.kind}</td>
                    <td className="px-3 py-1.5">
                      {run.workspace.slug}/{run.project.slug}
                    </td>
                    <td className="px-3 py-1.5">{when(run.created_at)}</td>
                    <td className="px-3 py-1.5">{runDuration(run)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Reveal>
      )}
    </div>
  );
}

import type { SystemNode } from '@/lib/command';
import { ToneGlyph } from './ToneGlyph';

export function NetworkMatrix({ nodes }: { nodes: readonly SystemNode[] }) {
  return (
    <div
      role="region"
      aria-label="Network status matrix"
      tabIndex={0}
      className="overflow-x-auto border border-shi-blue-slate bg-shi-grey"
    >
      <table className="w-full min-w-[36rem] border-collapse font-roboto-mono text-sm">
        <thead>
          <tr className="border-b border-shi-light text-left font-roboto">
            <th scope="col" className="px-3 py-1.5 font-medium">
              Node
            </th>
            <th scope="col" className="px-3 py-1.5 font-medium">
              State
            </th>
            <th scope="col" className="px-3 py-1.5 font-medium">
              Reported
            </th>
            <th scope="col" className="px-3 py-1.5 font-medium">
              Detail
            </th>
            <th scope="col" className="px-3 py-1.5 font-medium">
              Last seen
            </th>
          </tr>
        </thead>
        <tbody>
          {nodes.map((n) => (
            <tr key={n.id} className="border-b border-shi-blue-slate last:border-b-0">
              <th scope="row" className="px-3 py-1.5 text-left font-medium">
                <span className="block break-words">{n.name}</span>
                <span className="block font-normal text-shi-tint">{n.role}</span>
              </th>
              <td className="px-3 py-1.5">
                <span className="inline-flex items-center gap-2">
                  <ToneGlyph tone={n.tone} size={12} />
                  {n.state}
                </span>
              </td>
              <td
                className={`px-3 py-1.5 ${n.tone === 'good' ? 'text-shi-green' : 'text-shi-ink'}`}
              >
                {n.raw}
              </td>
              <td className="px-3 py-1.5 break-words text-shi-tint">{n.detail ?? 'None'}</td>
              <td className="px-3 py-1.5 text-shi-tint">
                {n.lastSeenAt ? (
                  <time dateTime={n.lastSeenAt}>{new Date(n.lastSeenAt).toLocaleString()}</time>
                ) : (
                  'Not reported'
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

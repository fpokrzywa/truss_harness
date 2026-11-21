import type { Diagnostic } from '@/lib/api/packaging-types';
import { Badge } from '../admin/ui';

/** One row per feature of a compatibility check: path, supported or not, and the reason. */
export function CompatibilityTable({
  title,
  diagnostics,
}: {
  title: string;
  diagnostics: Diagnostic[];
}) {
  return (
    <table className="w-full">
      <caption className="px-4 pt-4 text-left text-base font-semibold">{title}</caption>
      <thead className="border-b border-slate-200">
        <tr>
          {['Feature', 'Status', 'Reason'].map((h) => (
            <th key={h} scope="col" className="th">
              {h}
            </th>
          ))}
        </tr>
      </thead>
      <tbody className="divide-y divide-slate-100">
        {diagnostics.map((d, i) => (
          <tr key={`${d.path}-${i}`}>
            <td className="td font-mono text-xs break-all">{d.path}</td>
            <td className="td">
              {d.status === 'supported' ? (
                <Badge tone="good">Supported</Badge>
              ) : (
                <Badge tone="bad">Not supported</Badge>
              )}
            </td>
            <td className="td">{d.reason ?? '-'}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

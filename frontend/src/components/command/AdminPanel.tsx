import Link from 'next/link';
import type { AdminGroup } from '@/lib/command';
import { Reveal } from './Reveal';

export function AdminPanel({ groups }: { groups: readonly AdminGroup[] }) {
  if (groups.length === 0) {
    return (
      <p className="max-w-[65ch]">
        Your role has no administrative areas in this organization. Ask an organization admin for
        access.
      </p>
    );
  }
  return (
    <div className="grid gap-x-8 gap-y-3 md:grid-cols-3">
      {groups.map((group, g) => (
        <Reveal key={group.title} index={g}>
          <h3 className="mb-1 text-sm font-medium">{group.title}</h3>
          <ul className="flex flex-wrap gap-2">
            {group.items.map((item) => (
              <li key={item.id} className="min-w-[9rem] flex-1">
                <Link href={item.href} className="cc-admin-button">
                  {item.label}
                </Link>
              </li>
            ))}
          </ul>
        </Reveal>
      ))}
    </div>
  );
}

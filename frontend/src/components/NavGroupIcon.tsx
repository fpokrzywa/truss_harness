import type { ReactNode } from 'react';

const ICONS: Record<string, ReactNode> = {
  Home: <path d="M3 11l9-8 9 8M5 9.5V21h14V9.5M10 21v-6h4v6" />,
  Administer: (
    <>
      <path d="M4 6h10M18 6h2M4 12h2M10 12h10M4 18h8M16 18h4" />
      <circle cx="16" cy="6" r="2" />
      <circle cx="8" cy="12" r="2" />
      <circle cx="14" cy="18" r="2" />
    </>
  ),
  Security: <path d="M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6zM9 12l2 2 4-4" />,
  Integrate: <path d="M9 3v5M15 3v5M6 8h12v3a6 6 0 01-12 0zM12 17v4" />,
  Build: <path d="M3 13h8v8H3zM13 13h8v8h-8zM8 3h8v8H8z" />,
  Operate: (
    <>
      <circle cx="12" cy="12" r="9" />
      <path d="M10 8l6 4-6 4z" />
    </>
  ),
  Govern: <path d="M12 3v18M7 21h10M5 7h14M5 7l-3 7a3 3 0 006 0zM19 7l-3 7a3 3 0 006 0z" />,
};

/** Section icon for the closed navigation rail. A section without its own icon gets a plain dot. */
export function NavGroupIcon({ group }: { group: string }) {
  return (
    <svg
      aria-hidden="true"
      viewBox="0 0 24 24"
      width="20"
      height="20"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.75"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      {ICONS[group] ?? <circle cx="12" cy="12" r="3" />}
    </svg>
  );
}

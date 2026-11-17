'use client';

import { useState, type CSSProperties } from 'react';
import type { SystemNode } from '@/lib/command';
import { ToneGlyph } from './ToneGlyph';

const WIDTH = 600;
const TOP = 12;
/** Room under the bottom chord for the support triangles. */
const BELOW = 38;
/** Height of an equilateral triangle per unit of its base. */
const EQUILATERAL = Math.sqrt(3) / 2;

const delay = (i: number) => ({ '--cc-i': i }) as CSSProperties;

function chordClass(a: SystemNode, b: SystemNode): string {
  if (a.tone === 'warn' || b.tone === 'warn') return 'cc-member-fault';
  if (a.tone === 'idle' || b.tone === 'idle') return 'cc-member-idle';
  return 'cc-member-live';
}

/** A Warren truss whose bottom joints are the real components, centred over equal label columns. */
export function Truss({ nodes, flowing }: { nodes: readonly SystemNode[]; flowing: boolean }) {
  const [paused, setPaused] = useState(false);
  const BOTTOM = TOP + (WIDTH / nodes.length) * EQUILATERAL;
  const bottom = nodes.map((_, i) => ((i + 0.5) / nodes.length) * WIDTH);
  const top = bottom.slice(1).map((x, i) => (x + bottom[i]!) / 2);
  const zigzag = bottom.flatMap((x, i) => {
    const peak = top[i];
    return peak === undefined ? [`${x},${BOTTOM}`] : [`${x},${BOTTOM}`, `${peak},${TOP}`];
  });

  return (
    <div className="cc-truss" data-paused={paused}>
      <svg
        aria-hidden="true"
        viewBox={`0 0 ${WIDTH} ${BOTTOM + BELOW}`}
        className="block h-auto w-full"
      >
        {top.map((x, i) => (
          <g key={x}>
            <line
              pathLength={1}
              className="cc-member cc-member-web"
              style={delay(i * 2)}
              x1={bottom[i]}
              y1={BOTTOM}
              x2={x}
              y2={TOP}
            />
            <line
              pathLength={1}
              className="cc-member cc-member-web"
              style={delay(i * 2 + 1)}
              x1={x}
              y1={TOP}
              x2={bottom[i + 1]}
              y2={BOTTOM}
            />
            {top[i + 1] !== undefined && (
              <line
                pathLength={1}
                className="cc-member cc-member-web"
                style={delay(i * 2 + 2)}
                x1={x}
                y1={TOP}
                x2={top[i + 1]}
                y2={TOP}
              />
            )}
            <line
              pathLength={1}
              className={`cc-member ${chordClass(nodes[i]!, nodes[i + 1]!)}`}
              style={delay(i * 2 + 4)}
              x1={bottom[i]}
              y1={BOTTOM}
              x2={bottom[i + 1]}
              y2={BOTTOM}
            />
          </g>
        ))}
        {flowing && <polyline pathLength={1} className="cc-laser" points={zigzag.join(' ')} />}
        {[bottom[0]!, bottom[bottom.length - 1]!].map((x, i) => (
          <path
            key={i}
            d={`M${x} ${BOTTOM + 8} l14 24 h-28 Z`}
            className="fill-none stroke-shi-light"
          />
        ))}
        {top.map((x, i) => (
          <circle
            key={x}
            cx={x}
            cy={TOP}
            r="3"
            className="cc-joint fill-shi-slate stroke-shi-light"
            style={delay(i)}
          />
        ))}
        {nodes.map((n, i) => (
          <g
            key={n.id}
            className="cc-joint"
            style={delay(i * 2)}
            transform={`translate(${bottom[i]! - 10} ${BOTTOM - 10})`}
          >
            <rect width="20" height="20" className="fill-shi-slate" />
            <g transform="translate(3 3)">
              <ToneGlyph tone={n.tone} />
            </g>
          </g>
        ))}
      </svg>

      <ul
        className="mt-2 grid gap-y-2 text-sm sm:[grid-template-columns:repeat(var(--cc-n),minmax(0,1fr))]"
        style={{ '--cc-n': nodes.length } as CSSProperties}
      >
        {nodes.map((n) => (
          <li key={n.id} className="flex items-baseline gap-2 sm:block sm:text-center">
            <span className="mr-2 sm:hidden">
              <ToneGlyph tone={n.tone} size={12} />
            </span>
            <span className="block font-medium">{n.name}</span>
            <span className="block text-shi-tint">{n.state}</span>
          </li>
        ))}
      </ul>

      {flowing && (
        <button
          type="button"
          onClick={() => setPaused((p) => !p)}
          className="mt-6 inline-flex min-h-11 items-center border border-shi-light px-4 font-medium"
        >
          {paused ? 'Resume motion' : 'Pause motion'}
        </button>
      )}
    </div>
  );
}

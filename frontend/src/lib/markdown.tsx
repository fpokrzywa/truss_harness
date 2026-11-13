import type { ReactNode } from 'react';

/**
 * Minimal, safe markdown renderer (text/markdown only). It builds React elements from parsed text, so
 * every string is escaped by React: raw HTML in the source is shown as literal text, never parsed or
 * injected. No dangerouslySetInnerHTML anywhere. Links are limited to http(s) and mailto.
 */

const SAFE_URL = /^(https?:\/\/|mailto:)[^\s]+$/i;
const INLINE = /(`[^`]+`)|(\*\*[^*]+\*\*)|(\*[^*\s][^*]*\*)|(\[[^\]]+\]\([^)\s]+\))/g;

function inline(text: string, keyBase: string): ReactNode[] {
  const out: ReactNode[] = [];
  let last = 0;
  let i = 0;
  for (const m of text.matchAll(INLINE)) {
    const idx = m.index ?? 0;
    if (idx > last) out.push(text.slice(last, idx));
    const tok = m[0];
    const key = `${keyBase}-${i++}`;
    if (tok.startsWith('`')) {
      out.push(
        <code key={key} className="rounded bg-slate-100 px-1 font-mono text-xs">
          {tok.slice(1, -1)}
        </code>,
      );
    } else if (tok.startsWith('**')) {
      out.push(<strong key={key}>{tok.slice(2, -2)}</strong>);
    } else if (tok.startsWith('*')) {
      out.push(<em key={key}>{tok.slice(1, -1)}</em>);
    } else {
      const close = tok.indexOf('](');
      const label = tok.slice(1, close);
      const url = tok.slice(close + 2, -1);
      out.push(
        SAFE_URL.test(url) ? (
          <a
            key={key}
            href={url}
            rel="noopener noreferrer nofollow"
            target="_blank"
            className="text-blue-800 underline"
          >
            {label}
          </a>
        ) : (
          <span key={key}>{label}</span>
        ),
      );
    }
    last = idx + tok.length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

const UL = /^\s*[-*]\s+/;
const OL = /^\s*\d+[.)]\s+/;

export function Markdown({ source }: { source: string }) {
  const lines = source.replace(/\r\n/g, '\n').split('\n');
  const blocks: ReactNode[] = [];
  let i = 0;
  let k = 0;
  while (i < lines.length) {
    const line = lines[i] as string;
    if (line.trim() === '') {
      i++;
      continue;
    }
    if (line.trimStart().startsWith('```')) {
      const body: string[] = [];
      i++;
      while (i < lines.length && !(lines[i] as string).trimStart().startsWith('```')) {
        body.push(lines[i] as string);
        i++;
      }
      i++;
      blocks.push(
        <pre key={k++} className="overflow-x-auto rounded bg-slate-100 p-2 font-mono text-xs">
          <code>{body.join('\n')}</code>
        </pre>,
      );
      continue;
    }
    const h = /^(#{1,6})\s+(.*)$/.exec(line);
    if (h) {
      const level = (h[1] as string).length;
      const cls = level <= 2 ? 'text-lg font-semibold' : 'text-base font-semibold';
      const content = inline(h[2] as string, `h${k}`);
      blocks.push(
        level === 1 ? (
          <h3 key={k++} className={cls}>
            {content}
          </h3>
        ) : level === 2 ? (
          <h4 key={k++} className={cls}>
            {content}
          </h4>
        ) : (
          <h5 key={k++} className={cls}>
            {content}
          </h5>
        ),
      );
      i++;
      continue;
    }
    if (UL.test(line) || OL.test(line)) {
      const ordered = OL.test(line);
      const re = ordered ? OL : UL;
      const items: string[] = [];
      while (i < lines.length && re.test(lines[i] as string)) {
        items.push((lines[i] as string).replace(re, ''));
        i++;
      }
      const lis = items.map((t, n) => <li key={n}>{inline(t, `li${k}-${n}`)}</li>);
      blocks.push(
        ordered ? (
          <ol key={k++} className="list-decimal pl-5">
            {lis}
          </ol>
        ) : (
          <ul key={k++} className="list-disc pl-5">
            {lis}
          </ul>
        ),
      );
      continue;
    }
    if (line.startsWith('>')) {
      const q: string[] = [];
      while (i < lines.length && (lines[i] as string).startsWith('>')) {
        q.push((lines[i] as string).replace(/^>\s?/, ''));
        i++;
      }
      blocks.push(
        <blockquote key={k++} className="border-l-4 border-slate-300 pl-3 text-slate-700">
          {inline(q.join(' '), `q${k}`)}
        </blockquote>,
      );
      continue;
    }
    const para: string[] = [];
    while (
      i < lines.length &&
      (lines[i] as string).trim() !== '' &&
      !/^(#{1,6}\s|```|>|\s*[-*]\s|\s*\d+[.)]\s)/.test(lines[i] as string)
    ) {
      para.push(lines[i] as string);
      i++;
    }
    if (para.length === 0) {
      para.push(line); // guard against an infinite loop on an unmatched line start
      i++;
    }
    blocks.push(<p key={k++}>{inline(para.join(' '), `p${k}`)}</p>);
  }
  return <div className="space-y-2 text-sm text-slate-800">{blocks}</div>;
}

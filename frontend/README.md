# Front end

Next.js (App Router) + React 19 + TypeScript (strict) + Tailwind, pnpm, Vitest + Testing Library.

This is the application shell ported from SETH's front end: root layout, theme CSS, API client and
session plumbing, navigation, shared components, the `/api/*` proxy and the test setup. Module
pages are added one area per backend module; `docs/build/frontend-areas.md` maps every SETH
front-end file to its area.

```
pnpm install --frozen-lockfile
pnpm dev          # http://127.0.0.1:3710 ; /api/* is proxied to SHIFDE_API_ORIGIN (default http://127.0.0.1:8710)
pnpm lint
pnpm typecheck
pnpm test
pnpm build
pnpm e2e          # Playwright (Chromium): seeds this checkout's database, starts the API on 8780 and Next on 3780
```

`SHIFDE_API_ORIGIN` is read at request time by `src/proxy.ts`, never baked into the build.
`SHIFDE_WEB_API_PROXY` (`on`|`off`, default `on`, also read per request) turns that proxy off:
with `off` (or any value other than `on`) every `/api/*` request answers 404
`{"detail":"not found"}`. Production requires `off`, because the edge proxy routes `/api` to the API.
With the proxy on, only a path spelled exactly `/api` or `/api/...` and already canonical (no dot
segments, backslashes, repeated or trailing slashes, or double encoding) is forwarded, always to
`SHIFDE_API_ORIGIN`; any other spelling of an API path answers that same 404.
`src/proxy.ts` also gives every page a per-request nonce `Content-Security-Policy` (pages render per
request so Next puts the nonce on its scripts) plus `X-Content-Type-Options`, `Referrer-Policy`,
`X-Frame-Options` and `Cross-Origin-Opener-Policy`; HSTS belongs to the edge proxy.
Navigation is data-driven in `src/lib/nav.ts`; its links to area routes resolve as the areas land.

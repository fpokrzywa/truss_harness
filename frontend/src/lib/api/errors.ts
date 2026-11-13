/** RFC 9457 problem+json as emitted by the SHIFDE API (PHASE_1_CONTRACT). */
export interface ProblemFieldError {
  loc: (string | number)[];
  msg: string;
  type?: string;
}

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly title: string;
  readonly detail: string | undefined;
  readonly requestId: string | undefined;
  readonly fieldErrors: ProblemFieldError[];

  constructor(init: {
    status: number;
    code?: string;
    title?: string;
    detail?: string;
    requestId?: string;
    fieldErrors?: ProblemFieldError[];
  }) {
    const title = init.title ?? `Request failed (${init.status})`;
    super(init.detail ?? title);
    this.name = 'ApiError';
    this.status = init.status;
    this.code = init.code ?? 'unknown_error';
    this.title = title;
    this.detail = init.detail;
    this.requestId = init.requestId;
    this.fieldErrors = init.fieldErrors ?? [];
  }

  /** 403 or 404: render as "not found or no access" without revealing which. */
  get isNoAccess(): boolean {
    return this.status === 403 || this.status === 404;
  }
}

/** Transport failure (no HTTP response received). */
export class NetworkError extends Error {
  constructor(cause: unknown) {
    super('Could not reach the server. Check your connection and try again.', { cause });
    this.name = 'NetworkError';
  }
}

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null && !Array.isArray(v);
}

function str(v: unknown): string | undefined {
  return typeof v === 'string' ? v : undefined;
}

/** Build an ApiError from a failed response, tolerating non-problem bodies. */
export async function parseProblem(res: Response): Promise<ApiError> {
  const headerRequestId = res.headers.get('X-Request-ID') ?? undefined;
  let body: unknown;
  try {
    body = await res.json();
  } catch {
    body = undefined; // empty or non-JSON error body (e.g. proxy 502); fall back to status text
  }
  if (!isRecord(body)) {
    return new ApiError({
      status: res.status,
      title: res.statusText || undefined,
      requestId: headerRequestId,
    });
  }
  const rawErrors = Array.isArray(body.errors) ? (body.errors as unknown[]) : [];
  const fieldErrors: ProblemFieldError[] = [];
  for (const e of rawErrors) {
    if (isRecord(e) && typeof e.msg === 'string') {
      const loc = Array.isArray(e.loc)
        ? (e.loc as unknown[]).filter(
            (p): p is string | number => typeof p === 'string' || typeof p === 'number',
          )
        : [];
      fieldErrors.push({ loc, msg: e.msg, type: str(e.type) });
    }
  }
  return new ApiError({
    status: typeof body.status === 'number' ? body.status : res.status,
    code: str(body.code),
    title: str(body.title),
    detail: str(body.detail),
    requestId: str(body.request_id) ?? headerRequestId,
    fieldErrors,
  });
}

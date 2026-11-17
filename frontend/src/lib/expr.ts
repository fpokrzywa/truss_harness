/**
 * Client-side syntax check for the safe expression language shared by orchestrations (P5 section 4) and
 * pipelines (P6 amendment E1). It mirrors `backend/src/shifde/core/safeexpr.py`: literals, comparisons
 * (`== != < <= > >= in`, `not in`), boolean ops (`and or not`, also `&& || !`), parentheses and field
 * access on the evaluation context, either dotted (`a.b`) or indexed (`rows[0]`, `row['x']`). No calls.
 * The server stays authoritative: when unsure this check must accept rather than reject.
 */

type Tok = { t: 'num' | 'str' | 'id' | 'kw' | 'op'; v: string; pos: number };

/** A field path an expression reads, e.g. `['inputs', 'rollup', 0, 'max_open']`. */
export type ExprPath = (string | number)[];

export interface ExprResult {
  ok: boolean;
  error?: string;
  /** 0-based character offset of the problem. */
  pos?: number;
  /** On success: every field path the expression reads (for root / binding checks). */
  paths?: ExprPath[];
}

export const MAX_EXPR_LENGTH = 1000;

class ExprError extends Error {
  constructor(
    message: string,
    readonly pos: number,
  ) {
    super(message);
  }
}

const KEYWORDS = new Set(['and', 'or', 'not', 'in', 'true', 'false', 'null']);
const COMPARE = new Set(['==', '!=', '<', '<=', '>', '>=']);
// Same alternation order as the server tokenizer: number (with optional sign) before operators.
const TOKEN =
  /\s*(?:(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)|("(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')|(==|!=|<=|>=|&&|\|\||<|>|!|\(|\)|\[|\]|\.)|([A-Za-z][A-Za-z0-9_]*))/y;

function tokenize(src: string): Tok[] {
  if (src.length > MAX_EXPR_LENGTH)
    throw new ExprError(
      `Expression is longer than ${MAX_EXPR_LENGTH} characters.`,
      MAX_EXPR_LENGTH,
    );
  const out: Tok[] = [];
  let pos = 0;
  while (pos < src.length) {
    if (src.slice(pos).trim() === '') break;
    TOKEN.lastIndex = pos;
    const m = TOKEN.exec(src);
    if (m === null || TOKEN.lastIndex === pos) {
      const at = pos + (src.slice(pos).length - src.slice(pos).trimStart().length);
      const c = src[at] ?? '';
      if (c === '"' || c === "'") throw new ExprError('Unterminated string literal.', at);
      throw new ExprError(`Unexpected character "${c}".`, at);
    }
    const start = TOKEN.lastIndex - m[0].trimStart().length;
    if (m[1] !== undefined) out.push({ t: 'num', v: m[1], pos: start });
    else if (m[2] !== undefined) out.push({ t: 'str', v: m[2], pos: start });
    else if (m[3] !== undefined) out.push({ t: 'op', v: m[3], pos: start });
    else {
      const id = m[4] as string;
      out.push({ t: KEYWORDS.has(id) ? 'kw' : 'id', v: id, pos: start });
    }
    pos = TOKEN.lastIndex;
  }
  return out;
}

const unquote = (text: string): string =>
  text.slice(1, -1).replace(/\\(.)/g, (_m, c: string) => ({ n: '\n', t: '\t', r: '\r' })[c] ?? c);

class Parser {
  private i = 0;
  readonly paths: ExprPath[] = [];
  constructor(
    private readonly toks: Tok[],
    private readonly srcLength: number,
  ) {}

  private peek(): Tok | undefined {
    return this.toks[this.i];
  }
  private take(): Tok {
    const t = this.peek();
    if (!t) throw new ExprError('Expression ended unexpectedly.', this.srcLength);
    this.i++;
    return t;
  }
  private accept(t: Tok['t'], v?: string): boolean {
    const c = this.peek();
    if (c && c.t === t && (v === undefined || c.v === v)) {
      this.i++;
      return true;
    }
    return false;
  }

  parse(): void {
    this.or();
    const left = this.peek();
    if (left) throw new ExprError(`Unexpected "${left.v}".`, left.pos);
  }

  private or(): void {
    this.and();
    while (this.accept('kw', 'or') || this.accept('op', '||')) this.and();
  }
  private and(): void {
    this.not();
    while (this.accept('kw', 'and') || this.accept('op', '&&')) this.not();
  }
  private not(): void {
    if (this.accept('kw', 'not') || this.accept('op', '!')) {
      this.not();
      return;
    }
    this.compare();
  }
  private compare(): void {
    this.primary();
    const tok = this.peek();
    if (tok && ((tok.t === 'op' && COMPARE.has(tok.v)) || (tok.t === 'kw' && tok.v === 'in'))) {
      this.i++;
      this.primary();
      return;
    }
    const next = this.toks[this.i + 1];
    if (tok?.t === 'kw' && tok.v === 'not' && next?.t === 'kw' && next.v === 'in') {
      this.i += 2;
      this.primary();
    }
  }
  private primary(): void {
    const tok = this.take();
    if (tok.t === 'num' || tok.t === 'str') return;
    if (tok.t === 'kw' && ['true', 'false', 'null'].includes(tok.v)) return;
    if (tok.t === 'op' && tok.v === '(') {
      this.or();
      if (!this.accept('op', ')')) {
        throw new ExprError('Missing closing parenthesis.', this.peek()?.pos ?? this.srcLength);
      }
      return;
    }
    if (tok.t === 'id') {
      const path: ExprPath = [tok.v];
      for (;;) {
        if (this.accept('op', '.')) {
          const seg = this.take();
          if (seg.t !== 'id') throw new ExprError('Expected a field name after ".".', seg.pos);
          path.push(seg.v);
        } else if (this.accept('op', '[')) {
          const idx = this.take();
          if (idx.t === 'num' && /^\d+$/.test(idx.v)) path.push(Number(idx.v));
          else if (idx.t === 'str') path.push(unquote(idx.v));
          else throw new ExprError('An index must be a non-negative integer or a string.', idx.pos);
          if (!this.accept('op', ']')) throw new ExprError('Missing closing bracket.', idx.pos);
        } else break;
      }
      const next = this.peek();
      if (next && next.v === '(') throw new ExprError('Function calls are not allowed.', next.pos);
      this.paths.push(path);
      return;
    }
    throw new ExprError(`Unexpected "${tok.v}".`, tok.pos);
  }
}

export function checkExpression(src: string): ExprResult {
  if (src.trim() === '') return { ok: false, error: 'Expression is empty.', pos: 0 };
  try {
    const parser = new Parser(tokenize(src), src.length);
    parser.parse();
    return { ok: true, paths: parser.paths };
  } catch (e) {
    if (e instanceof ExprError) return { ok: false, error: e.message, pos: e.pos };
    throw e; // not a syntax problem: an internal invariant broke
  }
}

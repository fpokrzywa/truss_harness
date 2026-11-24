import { useState, type FormEvent } from 'react';
import { errorMessage, type AppApi, type ToolResult } from './api';
import type { TableScreen as TableScreenConfig } from './screens';

function cellText(cell: unknown): string {
  if (cell === null || cell === undefined) return '';
  if (typeof cell === 'object') return JSON.stringify(cell);
  return String(cell);
}

function rowCount(count: number): string {
  return count === 1 ? '1 row' : `${count} rows`;
}

export function TableScreen({ screen, api }: { screen: TableScreenConfig; api: AppApi }) {
  const [values, setValues] = useState<Record<string, string>>({});
  const [result, setResult] = useState<ToolResult | null>(null);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function run(event: FormEvent) {
    event.preventDefault();
    if (pending) return;
    const params: Record<string, string> = {};
    for (const name of screen.inputs) {
      const value = values[name] ?? '';
      if (value !== '') params[name] = value;
    }
    setPending(true);
    setError(null);
    setResult(null);
    try {
      setResult(await api.runTool(screen.tool, params));
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setPending(false);
    }
  }

  return (
    <section className="screen table">
      <h2>{screen.title}</h2>
      <form onSubmit={run} className="table-form">
        {screen.inputs.map((name) => (
          <label key={name}>
            {name}
            <input
              type="text"
              value={values[name] ?? ''}
              onChange={(event) => setValues({ ...values, [name]: event.target.value })}
            />
          </label>
        ))}
        <button type="submit" disabled={pending}>
          {pending ? 'Running...' : 'Run'}
        </button>
      </form>
      <p role="status" className="status">
        {pending ? 'Running...' : result ? rowCount(result.rows.length) : ''}
      </p>
      {error && (
        <p role="alert" className="error">
          {error}
        </p>
      )}
      {result && (
        <>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  {result.columns.map((column, index) => (
                    <th key={index} scope="col">
                      {column.name}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {result.rows.map((row, rowIndex) => (
                  <tr key={rowIndex}>
                    {row.map((cell, cellIndex) => (
                      <td key={cellIndex}>{cellText(cell)}</td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {result.truncated && (
            <p className="note">
              Showing the first {result.rows.length} rows: the result was truncated.
            </p>
          )}
        </>
      )}
    </section>
  );
}

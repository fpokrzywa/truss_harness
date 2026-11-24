import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { App } from './App';

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

const SCREENS = [
  { kind: 'chat', title: 'Ask', placeholder: 'Ask about orders' },
  { kind: 'table', title: 'Orders', tool: 'list_orders', inputs: ['region'] },
];

function mockFetch(...responses: (Response | Error)[]) {
  const fetchMock = vi.fn();
  for (const response of responses) {
    if (response instanceof Error) fetchMock.mockRejectedValueOnce(response);
    else fetchMock.mockResolvedValueOnce(response);
  }
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

afterEach(() => vi.unstubAllGlobals());

describe('App loading config/screens.json', () => {
  it('fetches the relative config and shows one navigation entry per screen', async () => {
    const fetchMock = mockFetch(jsonResponse(SCREENS));
    render(<App />);
    const nav = await screen.findByRole('navigation', { name: 'Screens' });
    expect(fetchMock).toHaveBeenCalledWith('config/screens.json');
    expect(nav.querySelectorAll('button')).toHaveLength(2);
    expect(screen.getByRole('button', { name: 'Ask' })).toHaveAttribute('aria-current', 'page');
    expect(screen.getByRole('heading', { name: 'Ask' })).toBeInTheDocument();
  });

  it('switches to another screen from the navigation', async () => {
    mockFetch(jsonResponse(SCREENS));
    render(<App />);
    await userEvent.click(await screen.findByRole('button', { name: 'Orders' }));
    expect(screen.getByRole('heading', { name: 'Orders' })).toBeInTheDocument();
    expect(screen.getByLabelText('region')).toBeInTheDocument();
  });

  it('shows an error and no screens when the fetch fails', async () => {
    mockFetch(new TypeError('offline'));
    render(<App />);
    expect(await screen.findByRole('alert')).toHaveTextContent('could not be loaded');
    expect(screen.queryByRole('navigation')).not.toBeInTheDocument();
  });

  it('shows an error when the config is not found', async () => {
    mockFetch(jsonResponse({ detail: 'missing' }, 404));
    render(<App />);
    expect(await screen.findByRole('alert')).toBeInTheDocument();
    expect(screen.queryByRole('navigation')).not.toBeInTheDocument();
  });

  it.each([
    ['an object', { screens: SCREENS }],
    ['an empty list', []],
    ['seven screens', Array.from({ length: 7 }, () => SCREENS[0])],
    ['an unknown kind', [{ kind: 'chart', title: 'Chart' }]],
    ['a table without inputs', [{ kind: 'table', title: 'T', tool: 't' }]],
    ['non-string inputs', [{ kind: 'table', title: 'T', tool: 't', inputs: [1] }]],
    ['a chat without a title', [{ kind: 'chat' }]],
  ])('shows an error for an invalid shape: %s', async (_label, body) => {
    mockFetch(jsonResponse(body));
    render(<App />);
    expect(await screen.findByRole('alert')).toBeInTheDocument();
    expect(screen.queryByRole('navigation')).not.toBeInTheDocument();
  });
});

import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { httpApi } from './api';
import { ChatScreen } from './ChatScreen';

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

const CHAT = { kind: 'chat' as const, title: 'Ask', placeholder: 'Ask about orders' };

afterEach(() => vi.unstubAllGlobals());

describe('ChatScreen', () => {
  it('posts the message to /api/ask and shows the question and the answer', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse({ answer: '42 orders', tool_calls: [] }));
    vi.stubGlobal('fetch', fetchMock);
    render(<ChatScreen screen={CHAT} api={httpApi} />);

    const input = screen.getByPlaceholderText('Ask about orders');
    await userEvent.type(input, 'How many orders?');
    await userEvent.click(screen.getByRole('button', { name: 'Send' }));

    expect(await screen.findByText('42 orders')).toBeInTheDocument();
    expect(screen.getByText('How many orders?')).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe('/api/ask');
    expect(init.method).toBe('POST');
    expect(init.headers).toEqual({ 'Content-Type': 'application/json' });
    expect(JSON.parse(init.body as string)).toEqual({ message: 'How many orders?' });
    expect(input).toHaveValue('');
  });

  it('disables send while the input is empty or a request is pending', async () => {
    let resolve: (value: Response) => void = () => {};
    vi.stubGlobal(
      'fetch',
      vi.fn().mockReturnValueOnce(new Promise<Response>((r) => (resolve = r))),
    );
    render(<ChatScreen screen={CHAT} api={httpApi} />);
    const send = screen.getByRole('button', { name: 'Send' });
    expect(send).toBeDisabled();

    await userEvent.type(screen.getByLabelText('Message'), 'hi');
    expect(send).toBeEnabled();
    await userEvent.click(send);
    expect(send).toBeDisabled();

    resolve(jsonResponse({ answer: 'hello', tool_calls: [] }));
    expect(await screen.findByText('hello')).toBeInTheDocument();
  });

  it('shows an error on a failed request and keeps the earlier conversation', async () => {
    vi.stubGlobal(
      'fetch',
      vi
        .fn()
        .mockResolvedValueOnce(jsonResponse({ answer: 'first answer', tool_calls: [] }))
        .mockResolvedValueOnce(jsonResponse({ detail: 'boom' }, 500))
        .mockRejectedValueOnce(new TypeError('offline')),
    );
    render(<ChatScreen screen={CHAT} api={httpApi} />);
    const input = screen.getByLabelText('Message');

    await userEvent.type(input, 'first');
    await userEvent.click(screen.getByRole('button', { name: 'Send' }));
    expect(await screen.findByText('first answer')).toBeInTheDocument();

    await userEvent.type(input, 'second');
    await userEvent.click(screen.getByRole('button', { name: 'Send' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('HTTP 500');
    expect(screen.getByText('first answer')).toBeInTheDocument();
    expect(screen.queryByText('second')).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole('button', { name: 'Send' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('The request failed.');
    expect(screen.getByText('first answer')).toBeInTheDocument();
  });
});

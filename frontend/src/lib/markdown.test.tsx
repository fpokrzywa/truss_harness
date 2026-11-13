import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { Markdown } from './markdown';

describe('Markdown (safe renderer)', () => {
  it('renders headings, emphasis, code and lists', () => {
    render(<Markdown source={'# Title\n\nSome **bold** and `code`.\n\n- one\n- two'} />);
    expect(screen.getByRole('heading', { name: 'Title' })).toBeInTheDocument();
    expect(screen.getByText('bold').tagName).toBe('STRONG');
    expect(screen.getByText('code').tagName).toBe('CODE');
    expect(screen.getAllByRole('listitem')).toHaveLength(2);
  });

  it('shows raw HTML as inert text, never as elements', () => {
    const { container } = render(
      <Markdown
        source={
          '<script>window.__xss = 1</script>\n\n<img src=x onerror="window.__xss=1">\n\n<b>hi</b>'
        }
      />,
    );
    expect(container.querySelector('script')).toBeNull();
    expect(container.querySelector('img')).toBeNull();
    expect(container.querySelector('b')).toBeNull();
    expect(container.textContent).toContain('<script>window.__xss = 1</script>');
    expect((window as unknown as { __xss?: number }).__xss).toBeUndefined();
  });

  it('only links http, https and mailto', () => {
    render(
      <Markdown
        source={'[ok](https://example.test/a) [bad](javascript:alert(1)) [data](data:text/html,x)'}
      />,
    );
    const ok = screen.getByRole('link', { name: 'ok' });
    expect(ok).toHaveAttribute('href', 'https://example.test/a');
    expect(ok).toHaveAttribute('rel', expect.stringContaining('noopener'));
    expect(screen.queryByRole('link', { name: 'bad' })).not.toBeInTheDocument();
    expect(screen.queryByRole('link', { name: 'data' })).not.toBeInTheDocument();
    expect(screen.getByText('bad')).toBeInTheDocument();
  });

  it('keeps fenced code literal', () => {
    const { container } = render(<Markdown source={'```\n<div onclick="x()">\n```'} />);
    expect(container.querySelector('div[onclick]')).toBeNull();
    expect(container.querySelector('pre')?.textContent).toBe('<div onclick="x()">');
  });
});

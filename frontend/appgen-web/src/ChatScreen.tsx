import { useState, type FormEvent } from 'react';
import { errorMessage, type AppApi } from './api';
import type { ChatScreen as ChatScreenConfig } from './screens';

type Message = { role: 'user' | 'assistant'; text: string };

export function ChatScreen({ screen, api }: { screen: ChatScreenConfig; api: AppApi }) {
  const [messages, setMessages] = useState<Message[]>([]);
  const [text, setText] = useState('');
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function send(event: FormEvent) {
    event.preventDefault();
    const question = text.trim();
    if (!question || pending) return;
    setPending(true);
    setError(null);
    try {
      const result = await api.ask(question);
      setMessages((prev) => [
        ...prev,
        { role: 'user', text: question },
        { role: 'assistant', text: result.answer },
      ]);
      setText('');
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setPending(false);
    }
  }

  return (
    <section className="screen chat">
      <h2>{screen.title}</h2>
      <ol className="messages" aria-label="Conversation">
        {messages.map((message, index) => (
          <li key={index} className={`message ${message.role}`}>
            {message.text}
          </li>
        ))}
      </ol>
      {error && (
        <p role="alert" className="error">
          {error}
        </p>
      )}
      <form onSubmit={send} className="chat-form">
        <input
          type="text"
          aria-label="Message"
          placeholder={screen.placeholder ?? ''}
          value={text}
          onChange={(event) => setText(event.target.value)}
        />
        <button type="submit" disabled={pending || !text.trim()}>
          Send
        </button>
      </form>
    </section>
  );
}

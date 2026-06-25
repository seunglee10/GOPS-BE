import { FormEvent, useState } from "react";
import { AlertCircle, Loader2, SendHorizontal } from "lucide-react";
import type { LlmInsight } from "../types/llm";

export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  text: string;
  createdAt: string;
}

interface ChatPanelProps {
  status: "idle" | "pending" | "success" | "error";
  error: string;
  messages: ChatMessage[];
  insights: LlmInsight[];
  onSend(message: string): Promise<void>;
}

export function ChatPanel({ status, error, messages, insights, onSend }: ChatPanelProps): JSX.Element {
  const [draft, setDraft] = useState("");
  const pending = status === "pending";

  async function handleSubmit(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    const message = draft.trim();
    if (!message || pending) return;
    setDraft("");
    await onSend(message);
  }

  return (
    <div className="panel chat-panel">
      <div className="panel-header">
        <h2>Chat</h2>
        <span className={`status-text status-${status}`}>{status}</span>
      </div>
      <div className="messages" aria-live="polite">
        {messages.length === 0 ? <p className="empty-state">No messages</p> : null}
        {messages.map((message) => (
          <article key={message.id} className={`message ${message.role}`}>
            <span>{message.role === "user" ? "You" : "AI"}</span>
            <p>{message.text}</p>
          </article>
        ))}
      </div>
      {insights.length > 0 ? (
        <div className="insights">
          {insights.map((insight) => (
            <div key={`${insight.title}-${insight.description}`} className={`insight insight-${insight.severity}`}>
              <strong>{insight.title}</strong>
              <p>{insight.description}</p>
            </div>
          ))}
        </div>
      ) : null}
      {error ? (
        <div className="chat-error" role="alert">
          <AlertCircle size={15} />
          <span>{error}</span>
        </div>
      ) : null}
      <form className="chat-form" onSubmit={handleSubmit}>
        <input value={draft} onChange={(event) => setDraft(event.target.value)} placeholder="Message" disabled={pending} />
        <button type="submit" disabled={pending || !draft.trim()} title="Send">
          {pending ? <Loader2 size={17} className="spin" /> : <SendHorizontal size={17} />}
          <span>Send</span>
        </button>
      </form>
    </div>
  );
}

import { useState } from "react";
import { MessageSquare, Search } from "lucide-react";

export default function ConversationHistory({
  conversations,
  active,
  disabled,
  onOpen,
}: {
  conversations: Record<string, any>[];
  active: string;
  disabled: boolean;
  onOpen: (conversation: Record<string, any>) => void;
}) {
  const [query, setQuery] = useState("");
  const matches = conversations.filter((item) =>
    String(item.title || "New conversation")
      .toLowerCase()
      .includes(query.trim().toLowerCase()),
  );
  return (
    <section className="conversation-history" aria-label="Conversation history">
      <h2>
        Conversations <span>{conversations.length}</span>
      </h2>
      <label className="conversation-search">
        <Search size={12} />
        <input
          aria-label="Find conversations"
          placeholder="Find a conversation…"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
        />
      </label>
      <div className="conversation-list">
        {matches.map((item) => (
          <button
            key={item.id}
            type="button"
            disabled={disabled}
            aria-current={item.id === active ? "page" : undefined}
            aria-label={`Open conversation: ${item.title || "New conversation"}`}
            title={item.title || "New conversation"}
            onClick={() => onOpen(item)}
          >
            <MessageSquare size={14} />
            <span>
              <strong>{item.title || "New conversation"}</strong>
              <small>
                {item.workflow_id ? "Workflow conversation" : "Conversation"}
              </small>
            </span>
          </button>
        ))}
        {!matches.length && (
          <p>
            {query
              ? "No matching conversations."
              : "Your conversations appear here after your first message."}
          </p>
        )}
      </div>
    </section>
  );
}

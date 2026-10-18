import { useState, useEffect, useRef } from "react";
import api from "../services/api";

export default function Advisor() {
  const [messages, setMessages] = useState([]); // {role: 'user'|'assistant', text}
  const [suggestions, setSuggestions] = useState([]);
  const [input, setInput] = useState("");
  const [isLoadingInitial, setIsLoadingInitial] = useState(true);
  const [isSending, setIsSending] = useState(false);
  const [error, setError] = useState(null);
  const bottomRef = useRef(null);

  useEffect(() => {
    api
      .get("/advisor/questions")
      .then((res) => setSuggestions(res.data.questions))
      .catch(() => setError("Не вдалося завантажити початкові запитання."))
      .finally(() => setIsLoadingInitial(false));
  }, []);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  async function askQuestion(question) {
    if (!question.trim() || isSending) return;
    setError(null);
    setMessages((prev) => [...prev, { role: "user", text: question }]);
    setSuggestions([]);
    setInput("");
    setIsSending(true);
    try {
      const res = await api.post("/advisor/answer", { question });
      setMessages((prev) => [...prev, { role: "assistant", text: res.data.answer }]);
      setSuggestions(res.data.follow_up || []);
    } catch (err) {
      setError(err.response?.data?.detail || "Не вдалося отримати відповідь.");
    } finally {
      setIsSending(false);
    }
  }

  function handleSubmit(e) {
    e.preventDefault();
    askQuestion(input);
  }

  return (
    <div className="flex h-[calc(100vh-8rem)] flex-col">
      <h1 className="mb-4 font-display text-2xl font-semibold text-forest">AI-радник</h1>

      <div className="flex-1 space-y-4 overflow-y-auto rounded-xl border border-ink/10 bg-white p-5">
        {isLoadingInitial ? (
          <p className="text-sm text-ink/50">Готуємо запитання на основі твоїх даних...</p>
        ) : messages.length === 0 ? (
          <p className="text-sm text-ink/50">
            Постав запитання про свій фінансовий стан, або обери одне з підказаних нижче.
          </p>
        ) : (
          messages.map((m, i) => (
            <div key={i} className={`flex ${m.role === "user" ? "justify-end" : "justify-start"}`}>
              <div
                className={`max-w-[80%] rounded-xl px-4 py-2.5 text-sm ${
                  m.role === "user"
                    ? "bg-forest text-paper"
                    : "border border-ink/10 bg-paper text-ink"
                }`}
              >
                {m.text}
              </div>
            </div>
          ))
        )}
        {isSending && <p className="text-xs text-ink/40">Радник думає...</p>}
        {error && <p className="rounded-lg bg-brick/10 px-3 py-2 text-sm text-brick">{error}</p>}
        <div ref={bottomRef} />
      </div>

      {suggestions.length > 0 && (
        <div className="mt-3 flex flex-wrap gap-2">
          {suggestions.map((q, i) => (
            <button
              key={i}
              onClick={() => askQuestion(q)}
              disabled={isSending}
              className="rounded-full border border-gold/40 bg-gold/10 px-3 py-1.5 text-xs text-ink transition hover:bg-gold/20 disabled:opacity-50"
            >
              {q}
            </button>
          ))}
        </div>
      )}

      <form onSubmit={handleSubmit} className="mt-3 flex gap-2">
        <input
          type="text"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder="Постав своє запитання..."
          disabled={isSending}
          className="flex-1 rounded-lg border border-ink/15 bg-white px-3 py-2.5 outline-none focus:border-forest focus:ring-1 focus:ring-forest disabled:opacity-50"
        />
        <button
          type="submit"
          disabled={isSending || !input.trim()}
          className="rounded-lg bg-forest px-5 py-2.5 font-medium text-paper transition hover:bg-forest-light disabled:opacity-50"
        >
          Надіслати
        </button>
      </form>
    </div>
  );
}

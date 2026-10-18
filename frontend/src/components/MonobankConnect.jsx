import { useState, useEffect, useRef } from "react";
import api from "../services/api";

export default function MonobankConnect({ onDone }) {
  const [step, setStep] = useState("token"); // token | accounts | syncing | done
  const [token, setToken] = useState("");
  const [error, setError] = useState(null);
  const [isSubmitting, setIsSubmitting] = useState(false);

  const [accounts, setAccounts] = useState([]);
  const [selectedAccounts, setSelectedAccounts] = useState([]);
  const [months, setMonths] = useState(1);

  const [jobId, setJobId] = useState(null);
  const [status, setStatus] = useState(null);
  const pollRef = useRef(null);

  async function handleConnect(e) {
    e.preventDefault();
    setError(null);
    setIsSubmitting(true);
    try {
      const res = await api.post("/mono/connect", { token });
      setAccounts(res.data.account_ids);
      setSelectedAccounts(res.data.account_ids); // за замовчуванням обрані всі
      setStep("accounts");
    } catch (err) {
      setError(err.response?.data?.detail || "Не вдалося підключити токен.");
    } finally {
      setIsSubmitting(false);
    }
  }

  async function handleStartSync() {
    setError(null);
    setIsSubmitting(true);
    try {
      const res = await api.post("/mono/sync/start", {
        account_ids: selectedAccounts,
        months,
      });
      setJobId(res.data.job_id);
      setStep("syncing");
    } catch (err) {
      setError(err.response?.data?.detail || "Не вдалося запустити синхронізацію.");
    } finally {
      setIsSubmitting(false);
    }
  }

  useEffect(() => {
    if (step !== "syncing" || !jobId) return;

    async function poll() {
      try {
        const res = await api.get(`/mono/sync/status/${jobId}`);
        setStatus(res.data);
        if (res.data.status === "completed") {
          setStep("done");
          clearInterval(pollRef.current);
        } else if (res.data.status === "failed") {
          setError(res.data.error_message || "Синхронізація не вдалась.");
          clearInterval(pollRef.current);
        }
      } catch {
        // мережева похибка одного опитування — не зупиняємо, спробуємо ще раз
      }
    }

    poll();
    pollRef.current = setInterval(poll, 4000);
    return () => clearInterval(pollRef.current);
  }, [step, jobId]);

  function toggleAccount(id) {
    setSelectedAccounts((prev) =>
      prev.includes(id) ? prev.filter((a) => a !== id) : [...prev, id]
    );
  }

  return (
    <div className="space-y-4">
      {step === "token" && (
        <form onSubmit={handleConnect} className="space-y-4">
          <div>
            <label className="mb-1 block text-sm font-medium text-ink">
              Персональний токен Monobank
            </label>
            <input
              type="text"
              required
              value={token}
              onChange={(e) => setToken(e.target.value)}
              className="w-full rounded-lg border border-ink/15 bg-white px-3 py-2.5 font-mono text-sm outline-none focus:border-forest focus:ring-1 focus:ring-forest"
              placeholder="Отримай на api.monobank.ua"
            />
            <p className="mt-1 text-xs text-ink/50">
              Токен зберігається зашифрованим і використовується тільки для читання виписки
            </p>
          </div>
          {error && <p className="rounded-lg bg-brick/10 px-3 py-2 text-sm text-brick">{error}</p>}
          <button
            type="submit"
            disabled={isSubmitting}
            className="w-full rounded-lg bg-forest px-4 py-2.5 font-medium text-paper transition hover:bg-forest-light disabled:opacity-50"
          >
            {isSubmitting ? "Перевіряємо..." : "Підключити"}
          </button>
        </form>
      )}

      {step === "accounts" && (
        <div className="space-y-4">
          <p className="text-sm text-ink/70">Обери рахунки для синхронізації:</p>
          <div className="space-y-2">
            {accounts.map((id) => (
              <label key={id} className="flex items-center gap-2 rounded-lg border border-ink/10 px-3 py-2">
                <input
                  type="checkbox"
                  checked={selectedAccounts.includes(id)}
                  onChange={() => toggleAccount(id)}
                  className="accent-forest"
                />
                <span className="font-mono text-xs text-ink/70">{id}</span>
              </label>
            ))}
          </div>
          <div>
            <label className="mb-1 block text-sm font-medium text-ink">
              Період історії (місяців)
            </label>
            <input
              type="number"
              min={1}
              max={24}
              value={months}
              onChange={(e) => setMonths(Number(e.target.value))}
              className="w-24 rounded-lg border border-ink/15 bg-white px-3 py-2 outline-none focus:border-forest focus:ring-1 focus:ring-forest"
            />
            <p className="mt-1 text-xs text-ink/50">
              Довша історія = точніший прогноз, але довша синхронізація (rate limit Monobank)
            </p>
          </div>
          {error && <p className="rounded-lg bg-brick/10 px-3 py-2 text-sm text-brick">{error}</p>}
          <button
            onClick={handleStartSync}
            disabled={isSubmitting || selectedAccounts.length === 0}
            className="w-full rounded-lg bg-forest px-4 py-2.5 font-medium text-paper transition hover:bg-forest-light disabled:opacity-50"
          >
            {isSubmitting ? "Запускаємо..." : "Почати синхронізацію"}
          </button>
        </div>
      )}

      {step === "syncing" && status && (
        <div className="space-y-3">
          <p className="text-sm text-ink/70">
            Синхронізуємо... (rate limit Monobank — приблизно 1 хвилина на місяць історії)
          </p>
          <div className="h-2 w-full overflow-hidden rounded-full bg-ink/10">
            <div
              className="h-full bg-gold transition-all"
              style={{
                width: `${status.chunks_total ? (status.chunks_done / status.chunks_total) * 100 : 0}%`,
              }}
            />
          </div>
          <p className="font-mono text-xs text-ink/50">
            {status.chunks_done} / {status.chunks_total} • {status.status}
          </p>
          {error && <p className="rounded-lg bg-brick/10 px-3 py-2 text-sm text-brick">{error}</p>}
        </div>
      )}

      {step === "done" && (
        <div className="space-y-4 text-center">
          <p className="font-display text-xl text-forest">Готово!</p>
          <p className="text-sm text-ink/70">Транзакції синхронізовано й категоризовано.</p>
          {status?.duplicates_removed > 0 && (
            <p className="text-sm text-ink/70">
              З них {status.duplicates_removed} виявилися дублікатами вже наявних транзакцій —
              видалено автоматично.
            </p>
          )}
          <button
            onClick={onDone}
            className="w-full rounded-lg bg-gold px-4 py-2.5 font-medium text-ink transition hover:bg-gold-light"
          >
            Перейти до Dashboard
          </button>
        </div>
      )}
    </div>
  );
}

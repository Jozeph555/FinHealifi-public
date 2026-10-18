import { useState, useEffect } from "react";
import api from "../services/api";

export default function ManualEntry({ onDone }) {
  const [categories, setCategories] = useState([]);
  const [incomeCategories, setIncomeCategories] = useState([]);
  const [date, setDate] = useState(() => new Date().toISOString().slice(0, 10));
  const [description, setDescription] = useState("");
  const [amount, setAmount] = useState("");
  const [category, setCategory] = useState("");
  const [error, setError] = useState(null);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [addedCount, setAddedCount] = useState(0);
  const [duplicateWarning, setDuplicateWarning] = useState(null);

  useEffect(() => {
    api.get("/transactions/categories").then((res) => {
      setCategories(res.data.categories);
      setIncomeCategories(res.data.income_categories || []);
    });
  }, []);

  const expenseCategories = categories.filter((c) => !incomeCategories.includes(c));

  async function submitTransaction(force) {
    setError(null);
    setIsSubmitting(true);
    try {
      const res = await api.post("/transactions/manual", {
        date: new Date(date).toISOString(),
        description,
        amount: Number(amount),
        category: category || null,
        force,
      });
      if (res.data.possible_duplicate) {
        setDuplicateWarning(res.data);
        return;
      }
      setDuplicateWarning(null);
      setAddedCount((c) => c + 1);
      setDescription("");
      setAmount("");
      setCategory("");
    } catch (err) {
      setError(err.response?.data?.detail || "Не вдалося додати транзакцію.");
    } finally {
      setIsSubmitting(false);
    }
  }

  function handleSubmit(e) {
    e.preventDefault();
    submitTransaction(false);
  }

  return (
    <div className="space-y-4">
      <form onSubmit={handleSubmit} className="space-y-4">
        <div>
          <label className="mb-1 block text-sm font-medium text-ink">Дата</label>
          <input
            type="date"
            required
            value={date}
            onChange={(e) => setDate(e.target.value)}
            className="w-full rounded-lg border border-ink/15 bg-white px-3 py-2.5 outline-none focus:border-forest focus:ring-1 focus:ring-forest"
          />
        </div>

        <div>
          <label className="mb-1 block text-sm font-medium text-ink">Опис</label>
          <input
            type="text"
            required
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            className="w-full rounded-lg border border-ink/15 bg-white px-3 py-2.5 outline-none focus:border-forest focus:ring-1 focus:ring-forest"
            placeholder="напр. Продукти в АТБ"
          />
        </div>

        <div>
          <label className="mb-1 block text-sm font-medium text-ink">
            Сума (грн, від'ємне = витрата)
          </label>
          <input
            type="number"
            step="0.01"
            required
            value={amount}
            onChange={(e) => setAmount(e.target.value)}
            className="w-full rounded-lg border border-ink/15 bg-white px-3 py-2.5 font-mono outline-none focus:border-forest focus:ring-1 focus:ring-forest"
            placeholder="-250.50"
          />
        </div>

        <div>
          <label className="mb-1 block text-sm font-medium text-ink">
            Категорія (необов'язково — визначимо автоматично)
          </label>
          <select
            value={category}
            onChange={(e) => setCategory(e.target.value)}
            className="w-full rounded-lg border border-ink/15 bg-white px-3 py-2.5 outline-none focus:border-forest focus:ring-1 focus:ring-forest"
          >
            <option value="">— автоматично —</option>
            <optgroup label="Витрати">
              {expenseCategories.map((c) => (
                <option key={c} value={c}>
                  {c}
                </option>
              ))}
            </optgroup>
            <optgroup label="Дохід">
              {incomeCategories.map((c) => (
                <option key={c} value={c}>
                  {c}
                </option>
              ))}
            </optgroup>
          </select>
        </div>

        {error && <p className="rounded-lg bg-brick/10 px-3 py-2 text-sm text-brick">{error}</p>}

        {duplicateWarning && (
          <div className="space-y-2 rounded-lg bg-gold/20 px-3 py-2 text-sm text-ink">
            <p>{duplicateWarning.message}</p>
            <div className="flex gap-2">
              <button
                type="button"
                onClick={() => submitTransaction(true)}
                disabled={isSubmitting}
                className="flex-1 rounded-lg bg-forest px-3 py-1.5 text-sm font-medium text-paper transition hover:bg-forest-light disabled:opacity-50"
              >
                Все одно додати
              </button>
              <button
                type="button"
                onClick={() => setDuplicateWarning(null)}
                className="flex-1 rounded-lg border border-ink/15 px-3 py-1.5 text-sm font-medium text-ink transition hover:bg-ink/5"
              >
                Скасувати
              </button>
            </div>
          </div>
        )}

        <button
          type="submit"
          disabled={isSubmitting}
          className="w-full rounded-lg bg-forest px-4 py-2.5 font-medium text-paper transition hover:bg-forest-light disabled:opacity-50"
        >
          {isSubmitting ? "Додаємо..." : "Додати транзакцію"}
        </button>
      </form>

      {addedCount > 0 && (
        <div className="space-y-2 border-t border-ink/10 pt-4 text-center">
          <p className="text-sm text-ink/70">Додано транзакцій: {addedCount}</p>
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

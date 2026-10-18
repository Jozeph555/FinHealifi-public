import { useState, useEffect } from "react";
import api from "../services/api";

function formatMoney(value) {
  if (value === null || value === undefined) return "—";
  return new Intl.NumberFormat("uk-UA", { maximumFractionDigits: 0 }).format(value) + " грн";
}

function formatDate(value) {
  // value — календарна дата "YYYY-MM-DD" без часу. new Date(value) трактує її
  // як UTC-опівніч і зсуває дату на день у часових поясах позаду UTC, тож
  // рядок розбирається напряму.
  if (!value) return "—";
  const [y, m, d] = value.split("-");
  return `${d}.${m}.${y}`;
}

const STATUS_LABELS = {
  not_repaid: "не повернено",
  partially_repaid: "частково повернено",
  repaid: "повернено",
};

const emptyForm = {
  borrower_name: "",
  principal_amount: "",
  issued_at: "",
};

function LoanFormFields({ form, setForm, showStatus }) {
  return (
    <>
      <div>
        <label className="mb-1 block text-sm font-medium text-ink">Кому позичено</label>
        <input
          type="text"
          required
          value={form.borrower_name}
          onChange={(e) => setForm({ ...form, borrower_name: e.target.value })}
          className="w-full rounded-lg border border-ink/15 bg-white px-3 py-2 outline-none focus:border-forest focus:ring-1 focus:ring-forest"
          placeholder="напр. Іван П."
        />
      </div>

      <div className="grid grid-cols-2 gap-3">
        <div>
          <label className="mb-1 block text-sm font-medium text-ink">Сума, грн</label>
          <input
            type="number"
            step="1"
            required
            value={form.principal_amount}
            onChange={(e) => setForm({ ...form, principal_amount: e.target.value })}
            className="w-full rounded-lg border border-ink/15 bg-white px-3 py-2 font-mono outline-none focus:border-forest focus:ring-1 focus:ring-forest"
          />
        </div>
        <div>
          <label className="mb-1 block text-sm font-medium text-ink">Дата видачі</label>
          <input
            type="date"
            required
            value={form.issued_at}
            onChange={(e) => setForm({ ...form, issued_at: e.target.value })}
            className="w-full rounded-lg border border-ink/15 bg-white px-3 py-2 outline-none focus:border-forest focus:ring-1 focus:ring-forest"
          />
        </div>
      </div>

      {showStatus && (
        <div className="grid grid-cols-2 gap-3">
          <div>
            <label className="mb-1 block text-sm font-medium text-ink">Залишок, грн</label>
            <input
              type="number"
              step="1"
              required
              value={form.remaining_amount}
              onChange={(e) => setForm({ ...form, remaining_amount: e.target.value })}
              className="w-full rounded-lg border border-ink/15 bg-white px-3 py-2 font-mono outline-none focus:border-forest focus:ring-1 focus:ring-forest"
            />
          </div>
          <div>
            <label className="mb-1 block text-sm font-medium text-ink">Статус</label>
            <select
              value={form.status}
              onChange={(e) => setForm({ ...form, status: e.target.value })}
              className="w-full rounded-lg border border-ink/15 bg-white px-3 py-2 outline-none focus:border-forest focus:ring-1 focus:ring-forest"
            >
              <option value="not_repaid">не повернено</option>
              <option value="partially_repaid">частково повернено</option>
              <option value="repaid">повернено</option>
            </select>
          </div>
        </div>
      )}
    </>
  );
}

export default function Loans() {
  const [loans, setLoans] = useState([]);
  const [isLoading, setIsLoading] = useState(true);

  const [form, setForm] = useState(emptyForm);
  const [formError, setFormError] = useState(null);
  const [isSubmitting, setIsSubmitting] = useState(false);

  const [editingId, setEditingId] = useState(null);
  const [editForm, setEditForm] = useState(null);
  const [editError, setEditError] = useState(null);
  const [isSavingEdit, setIsSavingEdit] = useState(false);

  async function loadAll() {
    setIsLoading(true);
    try {
      const res = await api.get("/loans");
      setLoans(res.data);
    } catch {
      setLoans([]);
    }
    setIsLoading(false);
  }

  useEffect(() => {
    loadAll();
  }, []);

  async function handleAddLoan(e) {
    e.preventDefault();
    setFormError(null);
    setIsSubmitting(true);
    try {
      await api.post("/loans", {
        borrower_name: form.borrower_name,
        principal_amount: Number(form.principal_amount),
        issued_at: form.issued_at,
      });
      setForm(emptyForm);
      await loadAll();
    } catch (err) {
      setFormError(err.response?.data?.detail || "Не вдалося додати позику.");
    } finally {
      setIsSubmitting(false);
    }
  }

  async function handleMarkRepaid(id) {
    if (!confirm("Позначити позику як повністю повернену?")) {
      return;
    }
    try {
      await api.patch(`/loans/${id}`, { status: "repaid", remaining_amount: 0 });
      await loadAll();
    } catch (err) {
      alert(err.response?.data?.detail || "Не вдалося оновити позику.");
    }
  }

  function startEdit(loan) {
    setEditError(null);
    setEditForm({
      borrower_name: loan.borrower_name,
      principal_amount: String(loan.principal_amount),
      issued_at: loan.issued_at,
      remaining_amount: String(loan.remaining_amount),
      status: loan.status,
    });
    setEditingId(loan.id);
  }

  function cancelEdit() {
    setEditingId(null);
    setEditForm(null);
    setEditError(null);
  }

  async function handleSaveEdit(e, id) {
    e.preventDefault();
    setEditError(null);
    setIsSavingEdit(true);
    try {
      await api.patch(`/loans/${id}`, {
        borrower_name: editForm.borrower_name,
        principal_amount: Number(editForm.principal_amount),
        issued_at: editForm.issued_at,
        remaining_amount: Number(editForm.remaining_amount),
        status: editForm.status,
      });
      cancelEdit();
      await loadAll();
    } catch (err) {
      setEditError(err.response?.data?.detail || "Не вдалося зберегти зміни.");
    } finally {
      setIsSavingEdit(false);
    }
  }

  if (isLoading) {
    return <p className="text-ink/50">Завантаження...</p>;
  }

  return (
    <div className="space-y-6">
      <h1 className="font-display text-2xl font-semibold text-forest">Позики</h1>

      {/* Список активних позик */}
      <section className="rounded-xl border border-ink/10 bg-white p-5">
        <h2 className="mb-4 text-sm font-medium uppercase tracking-wide text-ink/50">
          Активні позики
        </h2>
        {loans.length === 0 ? (
          <p className="text-sm text-ink/60">Ще немає жодної позики.</p>
        ) : (
          <div className="space-y-3">
            {loans.map((l) =>
              editingId === l.id ? (
                <form
                  key={l.id}
                  onSubmit={(e) => handleSaveEdit(e, l.id)}
                  className="space-y-3 rounded-lg border border-forest/30 bg-forest/5 p-3"
                >
                  <LoanFormFields form={editForm} setForm={setEditForm} showStatus />

                  {editError && (
                    <p className="rounded-lg bg-brick/10 px-3 py-2 text-sm text-brick">{editError}</p>
                  )}

                  <div className="flex gap-2">
                    <button
                      type="submit"
                      disabled={isSavingEdit}
                      className="flex-1 rounded-lg bg-forest px-4 py-2.5 font-medium text-paper transition hover:bg-forest-light disabled:opacity-50"
                    >
                      {isSavingEdit ? "Зберігаємо..." : "Зберегти"}
                    </button>
                    <button
                      type="button"
                      onClick={cancelEdit}
                      className="rounded-lg border border-ink/15 px-4 py-2.5 font-medium text-ink/60 transition hover:bg-ink/5"
                    >
                      Скасувати
                    </button>
                  </div>
                </form>
              ) : (
                <div
                  key={l.id}
                  className="flex items-center justify-between rounded-lg border border-ink/10 p-3"
                >
                  <div>
                    <p className="font-medium text-ink">{l.borrower_name}</p>
                    <p className="text-xs text-ink/50">
                      Видано {formatDate(l.issued_at)} • {STATUS_LABELS[l.status] || l.status}
                    </p>
                  </div>
                  <div className="flex items-center gap-3">
                    <span className="font-mono text-lg text-forest">{formatMoney(l.remaining_amount)}</span>
                    <button
                      onClick={() => startEdit(l)}
                      className="text-xs text-ink/40 hover:text-forest"
                    >
                      Редагувати
                    </button>
                    <button
                      onClick={() => handleMarkRepaid(l.id)}
                      className="text-xs text-ink/40 hover:text-brick"
                    >
                      Повністю повернено
                    </button>
                  </div>
                </div>
              )
            )}
          </div>
        )}
      </section>

      {/* Форма додавання позики */}
      <section className="rounded-xl border border-ink/10 bg-white p-5">
        <h2 className="mb-4 text-sm font-medium uppercase tracking-wide text-ink/50">
          Додати позику
        </h2>
        <form onSubmit={handleAddLoan} className="space-y-3">
          <LoanFormFields form={form} setForm={setForm} showStatus={false} />

          {formError && (
            <p className="rounded-lg bg-brick/10 px-3 py-2 text-sm text-brick">{formError}</p>
          )}

          <button
            type="submit"
            disabled={isSubmitting}
            className="w-full rounded-lg bg-forest px-4 py-2.5 font-medium text-paper transition hover:bg-forest-light disabled:opacity-50"
          >
            {isSubmitting ? "Додаємо..." : "Додати позику"}
          </button>
        </form>
      </section>
    </div>
  );
}

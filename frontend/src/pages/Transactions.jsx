import { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import api from "../services/api";
import {
  EDITABLE_GROUPS,
  GROUP_LABELS,
  METHOD_LABELS,
  allowedGroupsForAmount,
  displayGroups,
  errorDetail,
  formatAmountKopecks,
  formatKyivDate,
} from "../utils/transactions";

// Сторінка транзакцій: фільтри, сортування й пагінація через GET /transactions
// (X-Total-Count, X-Total-Amount) і ручна зміна категорії через PATCH
// /transactions/{id}.

const PAGE_SIZE = 50;
const SEARCH_DEBOUNCE_MS = 400;
const FILTER_KEYS = ["category", "sign", "from", "to", "q", "sort", "min_amount"];

// Пресет може задавати sort; без нього швидкий фільтр не чіпає обране сортування.
const QUICK_FILTERS = [
  { key: "all", label: "Усі", params: {} },
  { key: "review", label: "Потребують перегляду", params: { category: "pending" } },
  { key: "review_in", label: "Надходження без категорії", params: { category: "pending", sign: "in" } },
  { key: "big_pending", label: "Великі pending", params: { category: "pending", sign: "out", sort: "amount_desc" } },
];

const SORT_OPTIONS = [
  { value: "time_desc", label: "Нові спершу" },
  { value: "amount_desc", label: "Найбільші спершу" },
];

const SERVICE_LABELS = {
  pending: "Без категорії (pending)",
};

const inputClass =
  "rounded-lg border border-ink/15 bg-white px-3 py-2 text-sm outline-none focus:border-forest focus:ring-1 focus:ring-forest";

// Поле вводу, яке з debounce пише значення в query-параметр URL, а при
// навігації назад/вперед іде за URL.
function useDebouncedParam(searchParams, setParam, key) {
  const urlValue = searchParams.get(key) || "";
  const [value, setValue] = useState(urlValue);

  useEffect(() => {
    setValue(urlValue);
  }, [urlValue]);

  useEffect(() => {
    if (value === urlValue) return undefined;
    const timer = setTimeout(() => setParam(key, value), SEARCH_DEBOUNCE_MS);
    return () => clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [value]);

  return [value, setValue];
}

function lockReason(txn) {
  if (txn.loan_id) {
    return {
      badge: "Пов'язано з позикою",
      hint: "Транзакцію зараховано як повернення позики — звичайна зміна категорії зламала б цей зв'язок.",
    };
  }
  if (txn.category_method === "reimbursement") {
    return {
      badge: "Повернення витрати",
      hint: "Транзакцію позначено як повернення частки спільної витрати — вона віднімається від витрат категорії, тому звичайна зміна категорії недоступна.",
    };
  }
  return null;
}

function CategorySelect({ txn, groups, saving, onChange }) {
  const allowed = allowedGroupsForAmount(txn.amount);
  const allowedCategories = new Set(allowed.flatMap((g) => groups[g] || []));
  const current = txn.category && txn.category !== "pending" ? txn.category : "";
  // Поточна категорія, якої немає серед дозволених для знаку суми, лишається
  // видимою, але не обирається повторно.
  const groupsLoaded = Array.isArray(groups.expense);
  const currentIsForeign = current && !allowedCategories.has(current);

  return (
    <select
      value={current}
      disabled={saving}
      onChange={(e) => e.target.value && onChange(e.target.value)}
      className="w-full min-w-[11rem] rounded-md border border-ink/15 bg-white px-2 py-1 text-xs outline-none focus:border-forest focus:ring-1 focus:ring-forest disabled:opacity-50"
    >
      <option value="">— без категорії —</option>
      {currentIsForeign && (
        <option value={current} disabled>
          {groupsLoaded ? `${current} (не для цього знака)` : current}
        </option>
      )}
      {allowed.map((group) =>
        groups[group]?.length ? (
          <optgroup key={group} label={GROUP_LABELS[group]}>
            {groups[group].map((c) => (
              <option key={c} value={c}>
                {c}
              </option>
            ))}
          </optgroup>
        ) : null
      )}
    </select>
  );
}

export default function Transactions() {
  const [searchParams, setSearchParams] = useSearchParams();
  const [groups, setGroups] = useState({});
  const [rows, setRows] = useState([]);
  const [total, setTotal] = useState(0);
  const [totalAmount, setTotalAmount] = useState(0);
  const [isLoading, setIsLoading] = useState(true);
  const [loadError, setLoadError] = useState("");
  const [reloadKey, setReloadKey] = useState(0);
  const [savingId, setSavingId] = useState(null);
  const [rowErrors, setRowErrors] = useState({});
  const [newCategory, setNewCategory] = useState("");
  const [newCategoryError, setNewCategoryError] = useState("");
  const [newCategoryNote, setNewCategoryNote] = useState("");
  const [isCreatingCategory, setIsCreatingCategory] = useState(false);

  const page = Math.max(1, parseInt(searchParams.get("page") || "1", 10) || 1);
  const offset = (page - 1) * PAGE_SIZE;

  // Оновлює фільтри в URL; будь-яка зміна фільтра повертає на першу сторінку.
  // Функціональна форма — від актуального URL, а не від searchParams рендера:
  // відкладений (debounce) запис не відкотить фільтр, змінений за ці 400 мс.
  function updateParams(changes, { resetPage = true } = {}) {
    setSearchParams((prev) => {
      const next = new URLSearchParams(prev);
      for (const [key, value] of Object.entries(changes)) {
        if (value === undefined || value === null || value === "") next.delete(key);
        else next.set(key, value);
      }
      if (resetPage) next.delete("page");
      return next;
    });
  }

  const setParam = (key, value) => updateParams({ [key]: value });
  const [qInput, setQInput] = useDebouncedParam(searchParams, setParam, "q");
  const [minAmountInput, setMinAmountInput] = useDebouncedParam(searchParams, setParam, "min_amount");

  function applyQuickFilter(params) {
    setSearchParams((prev) => {
      const next = new URLSearchParams(prev);
      next.delete("category");
      next.delete("sign");
      next.delete("page");
      for (const [key, value] of Object.entries(params)) next.set(key, value);
      return next;
    });
  }

  const activeQuick = QUICK_FILTERS.find(
    (f) =>
      (searchParams.get("category") || "") === (f.params.category || "") &&
      (searchParams.get("sign") || "") === (f.params.sign || "") &&
      (!f.params.sort || (searchParams.get("sort") || "time_desc") === f.params.sort)
  )?.key;

  function loadCategories() {
    return api
      .get("/transactions/categories")
      .then((res) => setGroups(displayGroups(res.data)))
      .catch(() => setGroups({}));
  }

  useEffect(() => {
    loadCategories();
  }, []);

  async function handleCreateCategory(e) {
    e.preventDefault();
    setIsCreatingCategory(true);
    setNewCategoryError("");
    setNewCategoryNote("");
    try {
      const res = await api.post("/transactions/categories/custom", { name: newCategory });
      await loadCategories();
      setNewCategory("");
      setNewCategoryNote(`Категорію «${res.data.name}» додано до групи «Власні».`);
    } catch (err) {
      setNewCategoryError(errorDetail(err, "Не вдалося додати категорію."));
    } finally {
      setIsCreatingCategory(false);
    }
  }

  useEffect(() => {
    let cancelled = false;
    setIsLoading(true);
    setLoadError("");

    const params = { limit: PAGE_SIZE, offset };
    for (const key of FILTER_KEYS) {
      const value = (searchParams.get(key) || "").trim();
      if (value) params[key] = value;
    }
    // Бекенд приймає лише цілі гривні >= 0; недописане значення не надсилаємо.
    if (params.min_amount && !/^\d+$/.test(params.min_amount)) delete params.min_amount;

    api
      .get("/transactions", { params })
      .then((res) => {
        if (cancelled) return;
        setRows(res.data);
        setTotal(parseInt(res.headers["x-total-count"] ?? res.data.length, 10));
        setTotalAmount(parseInt(res.headers["x-total-amount"] ?? "0", 10));
        setRowErrors({});
      })
      .catch((err) => {
        if (cancelled) return;
        setRows([]);
        setTotal(0);
        setTotalAmount(0);
        setLoadError(errorDetail(err, "Не вдалося завантажити транзакції."));
      })
      .finally(() => {
        if (!cancelled) setIsLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [searchParams, offset, reloadKey]);

  async function handleCategoryChange(txn, category) {
    setSavingId(txn.id);
    setRowErrors((prev) => ({ ...prev, [txn.id]: "" }));
    try {
      // Без оптимістичного оновлення: рядок оновлюється лише з відповіді.
      const res = await api.patch(`/transactions/${txn.id}`, { category });
      setRows((prev) => prev.map((r) => (r.id === txn.id ? res.data : r)));
    } catch (err) {
      // select контрольований значенням рядка — він сам повертається до попереднього.
      setRowErrors((prev) => ({ ...prev, [txn.id]: errorDetail(err, "Не вдалося змінити категорію.") }));
    } finally {
      setSavingId(null);
    }
  }

  const filterCategoryOptions = [
    ...EDITABLE_GROUPS.map((g) => ({ group: g, items: groups[g] || [] })),
    { group: "service", items: groups.service || [] },
  ];
  const shownFrom = rows.length ? offset + 1 : 0;
  const shownTo = offset + rows.length;

  return (
    <div className="space-y-6">
      <h1 className="font-display text-2xl font-semibold text-forest">Транзакції</h1>

      {/* Швидкі фільтри */}
      <div className="flex flex-wrap gap-2">
        {QUICK_FILTERS.map((f) => (
          <button
            key={f.key}
            onClick={() => applyQuickFilter(f.params)}
            className={`rounded-lg px-3 py-1.5 text-sm font-medium transition ${
              activeQuick === f.key
                ? "bg-forest text-paper"
                : "border border-ink/15 bg-white text-ink/70 hover:bg-ink/5"
            }`}
          >
            {f.label}
          </button>
        ))}
      </div>

      {/* Детальні фільтри */}
      <section className="grid grid-cols-1 gap-3 rounded-xl border border-ink/10 bg-white p-4 sm:grid-cols-2 lg:grid-cols-3">
        <label className="text-xs text-ink/60">
          Категорія
          <select
            value={searchParams.get("category") || ""}
            onChange={(e) => updateParams({ category: e.target.value })}
            className={`mt-1 w-full ${inputClass}`}
          >
            <option value="">Усі категорії</option>
            {filterCategoryOptions.map(({ group, items }) =>
              items.length ? (
                <optgroup key={group} label={GROUP_LABELS[group]}>
                  {items.map((c) => (
                    <option key={c} value={c}>
                      {SERVICE_LABELS[c] || c}
                    </option>
                  ))}
                </optgroup>
              ) : null
            )}
          </select>
        </label>

        <label className="text-xs text-ink/60">
          Напрямок
          <select
            value={searchParams.get("sign") || ""}
            onChange={(e) => updateParams({ sign: e.target.value })}
            className={`mt-1 w-full ${inputClass}`}
          >
            <option value="">Усі</option>
            <option value="in">Надходження</option>
            <option value="out">Витрати</option>
          </select>
        </label>

        <label className="text-xs text-ink/60">
          Пошук в описі
          <input
            type="search"
            value={qInput}
            onChange={(e) => setQInput(e.target.value)}
            placeholder="напр. АТБ"
            className={`mt-1 w-full ${inputClass}`}
          />
        </label>

        <label className="text-xs text-ink/60">
          Від
          <input
            type="date"
            value={searchParams.get("from") || ""}
            onChange={(e) => updateParams({ from: e.target.value })}
            className={`mt-1 w-full ${inputClass}`}
          />
        </label>

        <label className="text-xs text-ink/60">
          До
          <input
            type="date"
            value={searchParams.get("to") || ""}
            onChange={(e) => updateParams({ to: e.target.value })}
            className={`mt-1 w-full ${inputClass}`}
          />
        </label>

        <label className="text-xs text-ink/60">
          Сортування
          <select
            value={searchParams.get("sort") || "time_desc"}
            onChange={(e) => updateParams({ sort: e.target.value === "time_desc" ? "" : e.target.value })}
            className={`mt-1 w-full ${inputClass}`}
          >
            {SORT_OPTIONS.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </select>
        </label>

        <label className="text-xs text-ink/60">
          Мінімальна сума, грн
          <input
            type="number"
            min="0"
            step="1"
            inputMode="numeric"
            value={minAmountInput}
            onChange={(e) => setMinAmountInput(e.target.value)}
            placeholder="напр. 5000"
            className={`mt-1 w-full ${inputClass}`}
          />
        </label>

        <div className="flex items-end">
          <button
            onClick={() => {
              setQInput("");
              setMinAmountInput("");
              setSearchParams(new URLSearchParams());
            }}
            className="w-full rounded-lg border border-ink/15 px-3 py-2 text-sm text-ink/60 transition hover:bg-ink/5"
          >
            Скинути фільтри
          </button>
        </div>
      </section>

      {!isLoading && !loadError && (
        <p className="text-sm text-ink/70">
          Знайдено <span className="font-medium text-ink">{total}</span> рядків на суму{" "}
          <span className="font-mono font-medium text-ink">
            {formatAmountKopecks(totalAmount, { withSign: false })}
          </span>
        </p>
      )}

      {/* Власна категорія витрат */}
      <form onSubmit={handleCreateCategory} className="rounded-xl border border-ink/10 bg-white p-4">
        <label className="text-xs text-ink/60">
          Нова власна категорія витрат
          <div className="mt-1 flex gap-2">
            <input
              type="text"
              value={newCategory}
              onChange={(e) => setNewCategory(e.target.value)}
              maxLength={60}
              placeholder="напр. Ремонт авто"
              className={`w-full ${inputClass}`}
            />
            <button
              type="submit"
              disabled={isCreatingCategory || !newCategory.trim()}
              className="whitespace-nowrap rounded-lg bg-forest px-4 py-2 text-sm font-medium text-paper transition hover:bg-forest-light disabled:opacity-50"
            >
              {isCreatingCategory ? "Додаємо..." : "Додати категорію"}
            </button>
          </div>
        </label>
        {newCategoryError && <p className="mt-2 text-xs text-brick">{newCategoryError}</p>}
        {newCategoryNote && <p className="mt-2 text-xs text-sage">{newCategoryNote}</p>}
      </form>

      {/* Таблиця */}
      <section className="rounded-xl border border-ink/10 bg-white">
        {isLoading ? (
          <p className="p-6 text-center text-sm text-ink/50">Завантаження...</p>
        ) : loadError ? (
          <div className="p-6 text-center">
            <p className="mb-3 text-sm text-brick">{loadError}</p>
            <button
              onClick={() => setReloadKey((k) => k + 1)}
              className="rounded-lg bg-forest px-4 py-2 text-sm font-medium text-paper transition hover:bg-forest-light"
            >
              Спробувати ще
            </button>
          </div>
        ) : rows.length === 0 ? (
          <p className="p-6 text-center text-sm text-ink/50">
            {total > 0 ? "На цій сторінці нічого немає — перейди на першу." : "Транзакцій за цими фільтрами немає."}
          </p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-left text-sm">
              <thead>
                <tr className="border-b border-ink/10 text-xs text-ink/50">
                  <th className="whitespace-nowrap px-3 py-2 font-medium">Дата</th>
                  <th className="px-3 py-2 font-medium">Опис</th>
                  <th className="whitespace-nowrap px-3 py-2 text-right font-medium">Сума</th>
                  <th className="px-3 py-2 font-medium">Категорія</th>
                  <th className="whitespace-nowrap px-3 py-2 font-medium">Як визначено</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((txn) => {
                  const lock = lockReason(txn);
                  return (
                    <tr key={txn.id} className="border-b border-ink/5 align-top last:border-0">
                      <td className="whitespace-nowrap px-3 py-2 font-mono text-xs text-ink/70">
                        {formatKyivDate(txn.time)}
                      </td>
                      <td className="px-3 py-2 text-ink/80">{txn.description || "—"}</td>
                      <td
                        className={`whitespace-nowrap px-3 py-2 text-right font-mono ${
                          txn.amount > 0 ? "text-sage" : txn.amount < 0 ? "text-brick" : "text-ink/60"
                        }`}
                      >
                        {formatAmountKopecks(txn.amount)}
                      </td>
                      <td className="px-3 py-2">
                        {lock ? (
                          <div title={lock.hint}>
                            <p className="text-xs text-ink/80">{txn.category}</p>
                            <span className="mt-1 inline-block cursor-help rounded bg-gold/15 px-1.5 py-0.5 text-[11px] text-ink/70">
                              {lock.badge}
                            </span>
                          </div>
                        ) : (
                          <CategorySelect
                            txn={txn}
                            groups={groups}
                            saving={savingId === txn.id}
                            onChange={(category) => handleCategoryChange(txn, category)}
                          />
                        )}
                        {rowErrors[txn.id] && (
                          <p className="mt-1 max-w-xs text-xs text-brick">{rowErrors[txn.id]}</p>
                        )}
                      </td>
                      <td className="whitespace-nowrap px-3 py-2">
                        <span
                          title={txn.category_method || ""}
                          className="rounded bg-ink/5 px-1.5 py-0.5 text-[11px] text-ink/60"
                        >
                          {METHOD_LABELS[txn.category_method] || txn.category_method || "—"}
                        </span>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {/* Пагінація */}
      {!loadError && (
        <div className="flex items-center justify-between text-sm text-ink/60">
          <span>
            Показано {shownFrom}–{shownTo} з {total}
          </span>
          <div className="flex gap-2">
            <button
              onClick={() => updateParams({ page: page > 2 ? String(page - 1) : "" }, { resetPage: false })}
              disabled={isLoading || page <= 1}
              className="rounded-lg border border-ink/15 px-3 py-1.5 transition hover:bg-ink/5 disabled:opacity-40"
            >
              ← Назад
            </button>
            <button
              onClick={() => updateParams({ page: String(page + 1) }, { resetPage: false })}
              disabled={isLoading || offset + PAGE_SIZE >= total}
              className="rounded-lg border border-ink/15 px-3 py-1.5 transition hover:bg-ink/5 disabled:opacity-40"
            >
              Вперед →
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

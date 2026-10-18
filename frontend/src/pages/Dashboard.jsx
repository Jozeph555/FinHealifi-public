import { useState, useEffect, useMemo } from "react";
import { Link } from "react-router-dom";
import api from "../services/api";
import { getLastFullMonthRange, getPeriodRange, monthsInRange, PERIOD_OPTIONS } from "../utils/dateRanges";

function formatMoney(value) {
  if (value === null || value === undefined) return "—";
  return new Intl.NumberFormat("uk-UA", { maximumFractionDigits: 0 }).format(value) + " грн";
}

// Значення до завантаження /transactions/categories; після завантаження
// замінюється на non_spending_categories з відповіді.
const FALLBACK_NON_SPENDING_CATEGORIES = ["pending", "Заощадження", "Інвестиції"];

function aggregateByCategory(transactions, start, end, nonSpendingCategories) {
  const byCategory = {};
  for (const t of transactions) {
    if (t.amount_uah >= 0 || !t.category || nonSpendingCategories.includes(t.category)) {
      continue;
    }
    const txDate = new Date(t.time * 1000);
    if (txDate < start || txDate > end) continue;
    byCategory[t.category] = (byCategory[t.category] || 0) + Math.abs(t.amount_uah);
  }
  return Object.entries(byCategory).sort((a, b) => b[1] - a[1]);
}

export default function Dashboard() {
  const [plan, setPlan] = useState(null);
  const [planError, setPlanError] = useState(false);
  const [transactions, setTransactions] = useState([]);
  const [nonSpendingCategories, setNonSpendingCategories] = useState(
    FALLBACK_NON_SPENDING_CATEGORIES
  );
  const [selectedPeriod, setSelectedPeriod] = useState("quarter");
  const [isLoading, setIsLoading] = useState(true);

  async function loadAll() {
    setIsLoading(true);

    const [planRes, txRes, categoriesRes] = await Promise.allSettled([
      api.get("/debt/plan/latest"),
      api.get("/transactions?limit=1000"),
      api.get("/transactions/categories"),
    ]);

    if (planRes.status === "fulfilled") {
      setPlan(planRes.value.data);
      setPlanError(false);
    } else {
      setPlanError(true);
    }

    if (txRes.status === "fulfilled") {
      setTransactions(txRes.value.data);
    }

    if (categoriesRes.status === "fulfilled") {
      setNonSpendingCategories(categoriesRes.value.data.non_spending_categories);
    }

    setIsLoading(false);
  }

  useEffect(() => {
    loadAll();
  }, []);

  const lastFullMonth = useMemo(() => getLastFullMonthRange(), []);
  const lastFullMonthCategories = useMemo(
    () => aggregateByCategory(transactions, lastFullMonth.start, lastFullMonth.end, nonSpendingCategories),
    [transactions, lastFullMonth, nonSpendingCategories]
  );
  const topCategory = lastFullMonthCategories[0] || null;

  const earliestTransactionDate = useMemo(() => {
    if (transactions.length === 0) return null;
    return new Date(Math.min(...transactions.map((t) => t.time * 1000)));
  }, [transactions]);

  const periodRange = useMemo(
    () => getPeriodRange(selectedPeriod, earliestTransactionDate),
    [selectedPeriod, earliestTransactionDate]
  );
  const periodMonthCount = monthsInRange(periodRange.start, periodRange.end);
  const periodCategoriesTotal = useMemo(
    () => aggregateByCategory(transactions, periodRange.start, periodRange.end, nonSpendingCategories),
    [transactions, periodRange, nonSpendingCategories]
  );
  const periodCategoriesAverage = periodCategoriesTotal.map(([name, total]) => [
    name,
    total / periodMonthCount,
  ]);

  const lastFullMonthLabel = new Intl.DateTimeFormat("uk-UA", {
    month: "long",
    year: "numeric",
  }).format(lastFullMonth.start);

  if (isLoading) {
    return <p className="text-ink/50">Завантаження...</p>;
  }

  return (
    <div className="space-y-6">
      <h1 className="font-display text-2xl font-semibold text-forest">
        Твій фінансовий стан
      </h1>

      {/* MILP-план */}
      <section className="rounded-xl border border-ink/10 bg-white p-5">
        <h2 className="mb-4 text-sm font-medium uppercase tracking-wide text-ink/50">
          План погашення боргів
        </h2>
        {planError ? (
          <div className="text-center">
            <p className="mb-3 text-sm text-ink/60">Плану ще немає.</p>
            <Link
              to="/debts"
              className="inline-block rounded-lg bg-forest px-4 py-2 text-sm font-medium text-paper transition hover:bg-forest-light"
            >
              Побудувати план на сторінці «Борги»
            </Link>
          </div>
        ) : (
          <div className="space-y-4">
            <div className="flex items-baseline justify-between">
              <div>
                <p className="text-xs text-ink/50">Місяців до повного погашення</p>
                <p className="font-mono text-2xl font-semibold text-forest">
                  {plan.months_to_payoff ?? "—"}
                </p>
              </div>
              <div className="text-right">
                <p className="text-xs text-ink/50">Переплата відсотків</p>
                <p className="font-mono text-lg text-ink">{formatMoney(plan.total_interest_paid)}</p>
              </div>
            </div>

            {/* Порівняння MILP і avalanche */}
            {plan.comparison?.avalanche && (
              <div className="space-y-2 border-t border-ink/10 pt-4">
                <p className="text-xs text-ink/50">Оптимальна стратегія проти популярної "лавини"</p>
                {["milp", "avalanche"].map((strategy) => {
                  const data = plan.comparison[strategy];
                  const maxInterest = Math.max(
                    plan.comparison.milp?.total_interest_paid || 0,
                    plan.comparison.avalanche?.total_interest_paid || 0
                  );
                  const widthPct = maxInterest > 0 ? (data.total_interest_paid / maxInterest) * 100 : 0;
                  return (
                    <div key={strategy}>
                      <div className="mb-1 flex justify-between text-xs">
                        <span className={strategy === "milp" ? "font-medium text-forest" : "text-ink/50"}>
                          {strategy === "milp" ? "MILP (оптимальна)" : "Лавина (avalanche)"}
                        </span>
                        <span className="font-mono">{formatMoney(data.total_interest_paid)}</span>
                      </div>
                      <div className="h-2 w-full overflow-hidden rounded-full bg-ink/5">
                        <div
                          className={`h-full rounded-full ${strategy === "milp" ? "bg-gold" : "bg-ink/20"}`}
                          style={{ width: `${widthPct}%` }}
                        />
                      </div>
                    </div>
                  );
                })}
                {plan.comparison.milp && plan.comparison.avalanche && (
                  <p className="pt-1 text-xs text-gold">
                    Економія:{" "}
                    {formatMoney(
                      plan.comparison.avalanche.total_interest_paid -
                        plan.comparison.milp.total_interest_paid
                    )}
                  </p>
                )}
              </div>
            )}
          </div>
        )}
      </section>

      {/* Найбільша категорія витрат за минулий повний місяць */}
      <section className="rounded-xl border border-ink/10 bg-white p-5">
        <h2 className="mb-4 text-sm font-medium uppercase tracking-wide text-ink/50">
          Витрати за {lastFullMonthLabel}
        </h2>

        {topCategory ? (
          <div className="mb-4 rounded-lg bg-gold/10 p-3">
            <p className="text-xs text-ink/50">Найбільша категорія</p>
            <div className="flex items-baseline justify-between">
              <p className="font-medium text-ink">{topCategory[0]}</p>
              <p className="font-mono font-semibold text-forest">{formatMoney(topCategory[1])}</p>
            </div>
          </div>
        ) : null}

        {lastFullMonthCategories.length === 0 ? (
          <p className="text-sm text-ink/60">Даних за {lastFullMonthLabel} ще немає.</p>
        ) : (
          <div className="space-y-2">
            {lastFullMonthCategories.slice(0, 6).map(([name, amount]) => (
              <div key={name}>
                <div className="mb-1 flex justify-between text-sm">
                  <span className="text-ink">{name}</span>
                  <span className="font-mono text-ink/70">{formatMoney(amount)}</span>
                </div>
                <div className="h-1.5 w-full overflow-hidden rounded-full bg-ink/5">
                  <div
                    className="h-full rounded-full bg-forest/70"
                    style={{ width: `${(amount / lastFullMonthCategories[0][1]) * 100}%` }}
                  />
                </div>
              </div>
            ))}
          </div>
        )}
      </section>

      {/* Середньомісячні витрати за обраний період */}
      <section className="rounded-xl border border-ink/10 bg-white p-5">
        <div className="mb-4 flex items-center justify-between">
          <h2 className="text-sm font-medium uppercase tracking-wide text-ink/50">
            Середньомісячні витрати
          </h2>
          <select
            value={selectedPeriod}
            onChange={(e) => setSelectedPeriod(e.target.value)}
            className="rounded-md border border-ink/15 bg-white px-2 py-1 text-sm outline-none focus:border-forest"
          >
            {PERIOD_OPTIONS.map((p) => (
              <option key={p.id} value={p.id}>
                {p.label}
              </option>
            ))}
          </select>
        </div>

        {periodCategoriesAverage.length === 0 ? (
          <p className="text-sm text-ink/60">Даних за цей період ще немає.</p>
        ) : (
          <div className="space-y-2">
            {periodCategoriesAverage.slice(0, 6).map(([name, avgAmount]) => (
              <div key={name}>
                <div className="mb-1 flex justify-between text-sm">
                  <span className="text-ink">{name}</span>
                  <span className="font-mono text-ink/70">{formatMoney(avgAmount)} / міс</span>
                </div>
                <div className="h-1.5 w-full overflow-hidden rounded-full bg-ink/5">
                  <div
                    className="h-full rounded-full bg-forest/70"
                    style={{ width: `${(avgAmount / periodCategoriesAverage[0][1]) * 100}%` }}
                  />
                </div>
              </div>
            ))}
          </div>
        )}
        <p className="mt-3 text-xs text-ink/40">
          Розраховано за {periodMonthCount} {periodMonthCount === 1 ? "місяць" : "місяців"}
        </p>
      </section>
    </div>
  );
}

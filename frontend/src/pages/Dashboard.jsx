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

// Частка переплати за «лавиною», нижче якої економія MILP вважається несуттєвою.
const NEGLIGIBLE_SAVINGS_SHARE = 0.01;

const STRATEGY_LABELS = {
  milp: "MILP (оптимальна)",
  avalanche: "Лавина (avalanche)",
  snowball: "Сніжок (snowball)",
  minimum: "Лише мінімальні платежі",
};

// Підпис для плану, що не погашає борги за горизонт планування.
function notPaidOffLabel(horizonMonths) {
  return horizonMonths ? `не погашається за ${horizonMonths} міс` : "не погашається";
}

function StrategyComparison({ comparison, horizonMonths }) {
  const milpInterest = comparison.milp.total_interest_paid;
  const avalancheInterest = comparison.avalanche.total_interest_paid;
  const nearAvalanche =
    avalancheInterest <= 0 ||
    avalancheInterest - milpInterest < NEGLIGIBLE_SAVINGS_SHARE * avalancheInterest;
  const strategies = (nearAvalanche ? ["milp", "snowball", "minimum"] : ["milp", "avalanche"]).filter(
    (strategy) => comparison[strategy]
  );
  const maxInterest = Math.max(...strategies.map((s) => comparison[s].total_interest_paid || 0));

  return (
    <div className="space-y-2 border-t border-ink/10 pt-4">
      <p className="text-xs text-ink/50">
        {nearAvalanche
          ? "Оптимальна стратегія проти «сніжка» і мінімальних платежів"
          : "Оптимальна стратегія проти популярної «лавини»"}
      </p>
      {strategies.map((strategy) => {
        const data = comparison[strategy];
        const widthPct = maxInterest > 0 ? (data.total_interest_paid / maxInterest) * 100 : 0;
        return (
          <div key={strategy}>
            <div className="mb-1 flex justify-between text-xs">
              <span className={strategy === "milp" ? "font-medium text-forest" : "text-ink/50"}>
                {STRATEGY_LABELS[strategy]}
                {data.months_to_payoff ? "" : ` — ${notPaidOffLabel(horizonMonths)}`}
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
      {nearAvalanche ? (
        <div className="space-y-1 pt-1 text-xs">
          {strategies
            .filter((strategy) => strategy !== "milp")
            .map((strategy) => (
              <p key={strategy} className="text-gold">
                Економія проти {strategy === "snowball" ? "«сніжка»" : "мінімальних платежів"}:{" "}
                {formatMoney(comparison[strategy].total_interest_paid - milpInterest)}
              </p>
            ))}
          <p className="text-ink/50">
            Оптимальний план практично збігається з «лавиною» (різниця переплати менше 1 %).
          </p>
        </div>
      ) : (
        <p className="pt-1 text-xs text-gold">
          Економія: {formatMoney(avalancheInterest - milpInterest)}
        </p>
      )}
    </div>
  );
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
                {plan.months_to_payoff ? (
                  <p className="font-mono text-2xl font-semibold text-forest">{plan.months_to_payoff}</p>
                ) : (
                  <p className="text-sm font-medium text-brick">{notPaidOffLabel(plan.horizon_months)}</p>
                )}
              </div>
              <div className="text-right">
                <p className="text-xs text-ink/50">Переплата відсотків</p>
                <p className="font-mono text-lg text-ink">{formatMoney(plan.total_interest_paid)}</p>
              </div>
            </div>

            {/* Порівняння MILP з евристиками */}
            {plan.comparison?.milp && plan.comparison?.avalanche && (
              <StrategyComparison comparison={plan.comparison} horizonMonths={plan.horizon_months} />
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

import { useState, useEffect, useMemo } from "react";
import { Link } from "react-router-dom";
import api from "../services/api";
import { formatUah } from "../utils/money";
import { getLastFullMonthRange, getPeriodRange, monthsInRange, PERIOD_OPTIONS } from "../utils/dateRanges";
import {
  DASHBOARD_NOT_MATERIALIZED_NOTE,
  MATCHES_AVALANCHE_NOTE,
  STRATEGY_LABELS,
  dashboardPlanView,
  isPaidOff,
  milpMatchesAvalanche,
  notPaidOffLabel,
} from "../utils/debtPlan";

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

// Частка переплати за «спочатку найдорожчий борг», нижче якої економія
// розрахованого плану вважається несуттєвою.
const NEGLIGIBLE_SAVINGS_SHARE = 0.01;

// Назви стратегій у формі «проти …» для рядків економії.
const STRATEGY_AGAINST_LABELS = {
  avalanche: "варіанту «спочатку найдорожчий борг»",
  snowball: "варіанту «спочатку найменший борг»",
  minimum: "лише обов'язкових платежів",
};

// caption — підпис, за якими залишками пораховано порівняння (null — без підпису).
function StrategyComparison({ comparison, horizonMonths, caption = null }) {
  const milpInterest = comparison.milp.total_interest_paid;
  const avalancheInterest = comparison.avalanche.total_interest_paid;
  const milpPaidOff = isPaidOff(comparison.milp);
  const nearAvalanche =
    milpPaidOff &&
    isPaidOff(comparison.avalanche) &&
    (avalancheInterest <= 0 ||
      avalancheInterest - milpInterest < NEGLIGIBLE_SAVINGS_SHARE * avalancheInterest);
  const matchesAvalanche = milpMatchesAvalanche(comparison);
  const strategies = (nearAvalanche ? ["milp", "snowball", "minimum"] : ["milp", "avalanche"]).filter(
    (strategy) => comparison[strategy]
  );
  const maxInterest = Math.max(
    0,
    ...strategies.filter((s) => isPaidOff(comparison[s])).map((s) => comparison[s].total_interest_paid || 0)
  );
  // Економію рахуємо лише між стратегіями, що обидві погашають борги.
  const savingsAgainst = strategies.filter(
    (strategy) => strategy !== "milp" && milpPaidOff && isPaidOff(comparison[strategy])
  );

  return (
    <div className="space-y-2 border-t border-ink/10 pt-4">
      <p className="text-xs text-ink/50">
        {nearAvalanche
          ? "Розрахований план проти варіанту «спочатку найменший борг» і лише обов'язкових платежів"
          : "Розрахований план проти варіанту «спочатку найдорожчий борг»"}
        {caption ? ` (${caption})` : ""}
      </p>
      {strategies.map((strategy) => {
        const data = comparison[strategy];
        const paidOff = isPaidOff(data);
        const widthPct = paidOff && maxInterest > 0 ? (data.total_interest_paid / maxInterest) * 100 : 0;
        return (
          <div key={strategy}>
            <div className="mb-1 flex justify-between gap-3 text-xs">
              <span className={`min-w-0 ${strategy === "milp" ? "font-medium text-forest" : "text-ink/50"}`}>
                {STRATEGY_LABELS[strategy]}
                {strategy === "milp" && matchesAvalanche ? ` — ${MATCHES_AVALANCHE_NOTE}` : ""}
              </span>
              {paidOff ? (
                <span className="shrink-0 whitespace-nowrap">
                  <span className="text-ink/50">Переплата </span>
                  <span className="font-mono">{formatUah(data.total_interest_paid)}</span>
                </span>
              ) : (
                <span className="text-brick">{notPaidOffLabel(horizonMonths)}</span>
              )}
            </div>
            {paidOff && (
              <div className="h-2 w-full overflow-hidden rounded-full bg-ink/5">
                <div
                  className={`h-full rounded-full ${strategy === "milp" ? "bg-gold" : "bg-ink/20"}`}
                  style={{ width: `${widthPct}%` }}
                />
              </div>
            )}
          </div>
        );
      })}
      <div className="space-y-1 pt-1 text-xs">
        {savingsAgainst.map((strategy) => (
          <p key={strategy} className="text-gold">
            Економія проти {STRATEGY_AGAINST_LABELS[strategy]}:{" "}
            {formatUah(comparison[strategy].total_interest_paid - milpInterest)}
          </p>
        ))}
        {nearAvalanche && !matchesAvalanche && (
          <p className="text-ink/50">
            Розрахований план практично збігається з варіантом «спочатку найдорожчий борг» (різниця
            переплати менше 1 %).
          </p>
        )}
      </div>
    </div>
  );
}

export default function Dashboard() {
  // Блок плану: прийнятий план (GET /debt/plan/active), а без нього — остання чернетка
  // (GET /debt/plan/latest); null — плану немає.
  const [planView, setPlanView] = useState(null);
  const [transactions, setTransactions] = useState([]);
  const [nonSpendingCategories, setNonSpendingCategories] = useState(
    FALLBACK_NON_SPENDING_CATEGORIES
  );
  const [selectedPeriod, setSelectedPeriod] = useState("quarter");
  const [isLoading, setIsLoading] = useState(true);

  async function loadAll() {
    setIsLoading(true);

    const [activeRes, planRes, txRes, categoriesRes] = await Promise.allSettled([
      api.get("/debt/plan/active"),
      api.get("/debt/plan/latest"),
      api.get("/transactions?limit=1000"),
      api.get("/transactions/categories"),
    ]);

    setPlanView(
      dashboardPlanView(
        activeRes.status === "fulfilled" ? activeRes.value.data : null,
        planRes.status === "fulfilled" ? planRes.value.data : null
      )
    );

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
        {!planView ? (
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
            <div className="space-y-1">
              <p
                className={`text-sm ${planView.kind === "accepted" ? "font-medium text-forest" : "text-ink/60"}`}
              >
                {planView.caption}
              </p>
              {planView.pendingText && <p className="text-sm text-ink/70">{planView.pendingText}</p>}
            </div>
            {!planView.materialized ? (
              <p className="text-sm text-ink/60">{DASHBOARD_NOT_MATERIALIZED_NOTE}</p>
            ) : (
              <div className="flex items-baseline justify-between gap-3">
                <div>
                  <p className="text-xs text-ink/50">Місяців до погашення</p>
                  {planView.monthsToPayoff ? (
                    <p className="font-mono text-2xl font-semibold text-forest">{planView.monthsToPayoff}</p>
                  ) : (
                    <p className="text-sm font-medium text-brick">{notPaidOffLabel(planView.horizonMonths)}</p>
                  )}
                </div>
                {planView.monthsToPayoff ? (
                  <div className="text-right">
                    <p className="text-xs text-ink/50">Переплата за планом</p>
                    <p className="whitespace-nowrap font-mono text-lg text-ink">{formatUah(planView.totalInterest)}</p>
                  </div>
                ) : null}
              </div>
            )}

            {/* Порівняння з евристиками — лише коли воно стосується показаного плану */}
            {planView.comparison?.milp && planView.comparison?.avalanche && (
              <StrategyComparison
                comparison={planView.comparison}
                horizonMonths={planView.horizonMonths}
                caption={planView.comparisonCaption}
              />
            )}
            {planView.comparisonNote && (
              <p className="border-t border-ink/10 pt-4 text-xs text-ink/60">
                {planView.comparisonNote}{" "}
                <Link to="/debts" className="text-forest underline decoration-forest/30 underline-offset-2">
                  Деталі — на сторінці «Борги»
                </Link>
              </p>
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
            <div className="flex items-baseline justify-between gap-3">
              <p className="min-w-0 font-medium text-ink [overflow-wrap:anywhere]">{topCategory[0]}</p>
              <p className="shrink-0 whitespace-nowrap font-mono font-semibold text-forest">{formatUah(topCategory[1])}</p>
            </div>
          </div>
        ) : null}

        {lastFullMonthCategories.length === 0 ? (
          <p className="text-sm text-ink/60">Даних за {lastFullMonthLabel} ще немає.</p>
        ) : (
          <div className="space-y-2">
            {lastFullMonthCategories.slice(0, 6).map(([name, amount]) => (
              <div key={name}>
                <div className="mb-1 flex justify-between gap-3 text-sm">
                  <span className="min-w-0 text-ink [overflow-wrap:anywhere]">{name}</span>
                  <span className="shrink-0 whitespace-nowrap font-mono text-ink/70">{formatUah(amount)}</span>
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
                <div className="mb-1 flex justify-between gap-3 text-sm">
                  <span className="min-w-0 text-ink [overflow-wrap:anywhere]">{name}</span>
                  <span className="shrink-0 whitespace-nowrap font-mono text-ink/70">{formatUah(avgAmount)} / міс</span>
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

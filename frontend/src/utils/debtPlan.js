// Підписи стратегій погашення і чисті функції для їх порівняння.

import { formatUah } from "./money.js";

// Ключі — як у plan_data / comparison з API; підписи — ті самі, що в раднику.
export const STRATEGY_LABELS = {
  milp: "Розрахований план",
  avalanche: "Спочатку найдорожчий борг",
  snowball: "Спочатку найменший борг",
  minimum: "Лише обов'язкові платежі",
};

export const MATCHES_AVALANCHE_NOTE = "збігається з варіантом «спочатку найдорожчий борг»";

// Переплати, що відрізняються не більше ніж на цю суму, вважаються однаковими.
const SAME_INTEREST_TOLERANCE_UAH = 1;

// Горизонт плану за замовчуванням, міс. (як PLAN_HORIZON_MONTHS на сервері).
export const DEFAULT_PLAN_HORIZON_MONTHS = 36;

// Підпис для стратегії, що не погашає борги за горизонт планування; без горизонту — за
// DEFAULT_PLAN_HORIZON_MONTHS.
export function notPaidOffLabel(horizonMonths) {
  return `не погашається за ${horizonMonths || DEFAULT_PLAN_HORIZON_MONTHS} міс.`;
}

// Чи погашає стратегія всі борги за горизонт (тільки тоді її переплату можна порівнювати).
export function isPaidOff(data) {
  return Boolean(data?.months_to_payoff);
}

// Чи збігається переплата розрахованого плану з «спочатку найдорожчий борг» у межах 1 грн.
export function milpMatchesAvalanche(comparison) {
  const milp = comparison?.milp;
  const avalanche = comparison?.avalanche;
  if (!isPaidOff(milp) || !isPaidOff(avalanche)) return false;
  return Math.abs(avalanche.total_interest_paid - milp.total_interest_paid) <= SAME_INTEREST_TOLERANCE_UAH;
}

// Порядок рядків порівняння: погашені стратегії за зростанням переплати, далі непогашені.
export function compareStrategies([, a], [, b]) {
  const paidA = isPaidOff(a);
  const paidB = isPaidOff(b);
  if (paidA !== paidB) return paidA ? -1 : 1;
  if (!paidA) return 0;
  return (a.total_interest_paid ?? Infinity) - (b.total_interest_paid ?? Infinity);
}

// --- Стани картки «План погашення» -------------------------------------------------------------
// Чернетка — результат «Побудувати план» (POST /debt/optimize, GET /debt/plan/latest);
// прийнятий план — відповідь GET /debt/plan/active: прийнята версія, effective (чинна в
// поточному місяці) і pending (почне діяти з наступного). Дати з API — рядки "YYYY-MM-DD"
// або "YYYY-MM"; порівнюються як рядки, без Date, щоб часовий пояс браузера не зсував день.
// Стан картки — набір прапорців, а не один стан: «ще не діє» і «склад змінився» можуть
// бути одночасно.

const MONTHS_GENITIVE = [
  "січня", "лютого", "березня", "квітня", "травня", "червня",
  "липня", "серпня", "вересня", "жовтня", "листопада", "грудня",
];

// Число, з якого прийняту версію можна зафіксувати без force (як MATERIALIZE_FROM_DAY на сервері).
export const MATERIALIZE_FROM_DAY = 6;

export const DRAFT_NOTE = "Цей план ще не прийнято. Прийми його, щоб порівнювати з фактичними платежами.";
export const DRAFT_KEEPS_ACCEPTED_NOTE = "Доки ти не приймеш цей план, чинним лишається прийнятий.";
export const DRAFT_STRUCTURE_CHANGED_NOTE = "Склад боргів змінився після побудови цього плану. Побудуй план ще раз.";
export const DRAFT_BALANCES_CHANGED_NOTE =
  "Залишки змінилися після побудови плану. Суми буде перераховано від фактичних залишків на початок місяця дії.";
export const BUILD_TIME_BALANCES_CAPTION = "за залишками на момент побудови";
export const STRUCTURE_CHANGED_NOTE =
  "Склад боргів змінився після прийняття плану. Щоб план їх враховував, побудуй і прийми новий план.";
export function addedDebtText(name) {
  return `У план включено борг «${name}», якого не було в прийнятій версії. ` +
    "Якщо хочеш інший розподіл, побудуй і прийми новий план.";
}
export function removedDebtText(name) {
  return `З плану прибрано борг «${name}», бо його закрито або деактивовано.`;
}
export const NO_SCHEDULE_TOTALS_NOTE = "Суми на місяць з'являться, коли план буде зафіксовано.";
export const MATERIALIZE_BUTTON_LABEL = "Зафіксувати попередній місяць";

const ACCEPT_STALE_TEXT = "Борги змінилися після побудови плану. Побудуй план ще раз.";
const SOLVER_LATE_TEXT = "Розрахунок не встиг. Спробуй ще раз пізніше.";

// "YYYY-MM-DD" -> "4 листопада 2026"; "YYYY-MM" -> перше число місяця. Інше — "—" або як є.
export function formatDateUk(value) {
  if (value === null || value === undefined || value === "") return "—";
  const match = /^(\d{4})-(\d{2})(?:-(\d{2}))?$/.exec(String(value));
  if (!match) return String(value);
  const month = Number(match[2]);
  const day = match[3] ? Number(match[3]) : 1;
  if (month < 1 || month > 12 || day < 1 || day > 31) return String(value);
  return `${day} ${MONTHS_GENITIVE[month - 1]} ${match[1]}`;
}

// Сьогоднішня дата за Europe/Kyiv як "YYYY-MM-DD" (межі місяців на сервері — за Києвом).
export function todayKyiv(now = new Date()) {
  return new Intl.DateTimeFormat("en-CA", {
    timeZone: "Europe/Kyiv",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(now);
}

// Дата «сьогодні» для плану: з сервера (GET /debt/today — Europe/Kyiv або APP_TODAY_OVERRIDE), а якщо
// її ще немає чи запит не вдався — todayKyiv() браузера.
export function resolveToday(serverToday, now = new Date()) {
  const value = serverToday?.today;
  return typeof value === "string" && /^\d{4}-\d{2}-\d{2}$/.test(value) ? value : todayKyiv(now);
}

// Помітка демонстраційної дати (сервер запущено з APP_TODAY_OVERRIDE); null — дата реальна.
export function demoDateText(serverToday) {
  return serverToday?.demo === true && serverToday.today ? `Демонстраційна дата: ${serverToday.today}` : null;
}

// Чи чернетка — джерело вже прийнятої версії (тоді окремого блоку чернетки немає).
function draftIsAccepted(draft, active) {
  if (!draft || !active) return false;
  return [active, active.effective, active.pending].some((v) => v && v.source_plan_id === draft.plan_id);
}

// Блок чернетки: null — чернетки немає або її вже прийнято; інакше
// { status, notes, totals, showNumbers, numbersCaption, canAccept }.
// status: "ok" | "balances_changed" | "structure_changed". Першу чернетку (прийнятого плану
// ще немає) сервер приймає лише без жодних змін, тож для неї зміна залишків — як зміна складу.
export function draftView(draft, active) {
  if (!draft || draftIsAccepted(draft, active)) return null;
  const hasAccepted = Boolean(active);
  let status = "ok";
  if (draft.structure_changed === true || (!hasAccepted && draft.balances_changed === true)) {
    status = "structure_changed";
  } else if (draft.balances_changed === true) {
    status = "balances_changed";
  }
  const keepsAccepted = hasAccepted ? [DRAFT_KEEPS_ACCEPTED_NOTE] : [];
  if (status === "structure_changed") {
    return {
      status,
      notes: [DRAFT_STRUCTURE_CHANGED_NOTE],
      totals: { min: null, extra: null, total: null },
      showNumbers: false,
      numbersCaption: null,
      canAccept: false,
    };
  }
  if (status === "balances_changed") {
    return {
      status,
      notes: [DRAFT_BALANCES_CHANGED_NOTE, ...keepsAccepted],
      totals: { min: null, extra: draft.extra_monthly_uah ?? null, total: null },
      showNumbers: true,
      numbersCaption: BUILD_TIME_BALANCES_CAPTION,
      canAccept: true,
    };
  }
  return {
    status,
    notes: [DRAFT_NOTE, ...keepsAccepted],
    totals: draftTotals(draft),
    showNumbers: true,
    numbersCaption: null,
    canAccept: true,
  };
}

export const BUILD_TIME_PREVIEW_CAPTION = "за залишками на момент побудови";

// Речення про очікувану версію: «Новий план (додатковий платіж X) почне діяти з …». Якщо версія
// ще без розкладу й має preview (числа її чернетки), додає «: орієнтовно N міс., переплата X
// (за залишками на момент побудови)».
export function pendingPlanText(pending) {
  const extra = pending.extra_monthly;
  const extraText = extra === null || extra === undefined ? "" : ` (додатковий платіж ${formatUah(extra)})`;
  const base = `Новий план${extraText} почне діяти з ${formatDateUk(pending.effective_from)}`;
  const preview = pending.materialized ? null : pending.preview;
  if (!preview) return `${base}.`;
  const numbers = preview.months_to_payoff
    ? `орієнтовно ${preview.months_to_payoff} міс., переплата ${formatUah(preview.total_interest)}`
    : `за розрахунком ${notPaidOffLabel(preview.horizon_months)}`;
  return `${base}: ${numbers} (${BUILD_TIME_PREVIEW_CAPTION})`;
}

// Підпис блоку з числами останньої чернетки на сторінці «Борги» (місяці, переплата, графік,
// порівняння): «Чернетка», поки її не прийнято; «Останній побудований план, очікує початку дії з …»,
// якщо з неї прийнято очікувану версію; «Розрахунок прийнятого плану, діє з …», якщо з неї прийнято
// чинну версію. null — чернетки немає.
export function draftDetailsCaption(draft, active) {
  if (!draft) return null;
  const effective = active?.effective ?? null;
  const pending = active?.pending ?? null;
  if (pending && pending.source_plan_id === draft.plan_id) {
    return `Останній побудований план, очікує початку дії з ${formatDateUk(pending.effective_from)}`;
  }
  const accepted = [effective, active].find((v) => v && v.source_plan_id === draft.plan_id);
  if (accepted) return `Розрахунок прийнятого плану, діє з ${formatDateUk(accepted.effective_from)}`;
  return "Чернетка";
}

// Повідомлення про склад боргів для прийнятого плану (GET /debt/plan/active). Остання прийнята
// версія — очікувана (pending), а без неї — чинна (effective, або сама відповідь у старому форматі).
// Остання прийнята розходиться зі складом → STRUCTURE_CHANGED_NOTE («побудуй і прийми новий план»);
// розходиться лише чинна, а очікувана вже відповідає складу → борги увійдуть у план з дати
// очікуваної; інакше null. Повертає { key, text } або null.
export function structureNotice(active) {
  if (!active) return null;
  const effective = active.effective ?? null;
  const pending = active.pending ?? null;
  const latest = pending ?? effective ?? active;
  if (latest.structure_changed === true) return { key: "structure_changed", text: STRUCTURE_CHANGED_NOTE };
  if (pending && effective?.structure_changed === true) {
    return {
      key: "structure_covered_by_pending",
      text:
        `Борги, додані після прийняття діючого плану, увійдуть у план з ${formatDateUk(pending.effective_from)}: ` +
        "новий план їх враховує.",
    };
  }
  return null;
}

// Блок прийнятого плану: null — прийнятого плану немає; інакше
// { shown, pending, flags: { notYetInForce, structureChanged }, messages: [{ key, text }] }.
// shown — версія, чиї суми й розклад показуються: чинна в поточному місяці, а якщо її немає —
// прийнята. messages: "in_force" (з якої дати діє), "pending" (новий план почне діяти),
// "added_debt:<id>" / "removed_debt:<id>" (склад розкладу shown відрізняється від прийнятої
// чернетки: added_debts / removed_debts з API), "structure_changed" або
// "structure_covered_by_pending" (structureNotice). flags.structureChanged — остання прийнята
// версія розходиться зі складом боргів.
export function acceptedPlanView(active, today) {
  if (!active) return null;
  const effective = active.effective ?? null;
  const pending = active.pending ?? (active.effective_from > today ? active : null);
  const shown = effective ?? active;
  const flags = {
    notYetInForce: Boolean(pending),
    structureChanged: structureNotice(active)?.key === "structure_changed",
  };
  const messages = [];
  if (effective) {
    messages.push({ key: "in_force", text: `Діє з ${formatDateUk(effective.effective_from)}` });
  }
  if (pending) messages.push({ key: "pending", text: pendingPlanText(pending) });
  for (const d of shown.added_debts ?? []) {
    messages.push({ key: `added_debt:${d.debt_id}`, text: addedDebtText(d.name) });
  }
  for (const d of shown.removed_debts ?? []) {
    messages.push({ key: `removed_debt:${d.debt_id}`, text: removedDebtText(d.name) });
  }
  const notice = structureNotice(active);
  if (notice) messages.push(notice);
  return { shown, pending, flags, messages };
}

// Планові платежі версії за поточний місяць: [{ debt_id, name, payment, closing }] за спаданням
// платежу, без нульових; null — у розкладі немає поточного місяця.
export function currentMonthPayments(version, today) {
  const month = today.slice(0, 7);
  const entry = version?.schedule?.find((m) => m.month === month);
  if (!entry) return null;
  return entry.debts
    .filter((d) => d.payment > 0)
    .map((d) => ({ debt_id: d.debt_id, name: d.name, payment: d.payment, closing: d.closing }))
    .sort((a, b) => b.payment - a.payment);
}

// Що робити з прийнятою версією під час відкриття сторінки:
// "none" — розклад уже є або версія ще не діє; "auto" — викликати POST /debt/plan/materialize;
// "too_early" — до MATERIALIZE_FROM_DAY-го числа: пояснення і кнопка з force=true.
export function materializeAction(active, today) {
  if (!active || active.materialized) return "none";
  if (active.effective_from > today) return "none";
  return Number(today.slice(8, 10)) < MATERIALIZE_FROM_DAY ? "too_early" : "auto";
}

// Пояснення, коли фіксувати ще рано.
export function materializeTooEarlyText(active) {
  return (
    `План почне діяти з ${formatDateUk(active.effective_from)}; зафіксувати можна з ` +
    `${MATERIALIZE_FROM_DAY}-го числа або за кнопкою «${MATERIALIZE_BUTTON_LABEL}».`
  );
}

// Текст помилки прийняття чернетки за HTTP-статусом і текстом відповіді.
export function acceptErrorText(status, detail) {
  if (status === 409) return ACCEPT_STALE_TEXT;
  if (status === 503) return SOLVER_LATE_TEXT;
  if (status === 422 && detail) return detail;
  return "Не вдалося прийняти план" + (status ? ` (HTTP ${status})` : "") + (detail ? `: ${detail}` : ".");
}

// Текст помилки фіксації: 409 і 422 — текст відповіді, 503 — як для прийняття.
export function materializeErrorText(status, detail) {
  if (status === 503) return SOLVER_LATE_TEXT;
  if ((status === 409 || status === 422) && detail) return detail;
  return "Не вдалося зафіксувати план" + (status ? ` (HTTP ${status})` : "") + (detail ? `: ${detail}` : ".");
}

// Суми «на місяць» у спільному вигляді: { min, extra, total } (гривні або null).
export function draftTotals(plan) {
  return {
    min: plan?.min_payments_total_uah ?? null,
    extra: plan?.extra_monthly_uah ?? null,
    total: plan?.total_monthly_payment_uah ?? null,
  };
}

export function versionTotals(active) {
  return {
    min: active?.min_payments_total ?? null,
    extra: active?.extra_monthly ?? null,
    total: active?.total_monthly_payment ?? null,
  };
}

// --- План на Dashboard ---------------------------------------------------------------------------

export const DASHBOARD_DRAFT_CAPTION = "Чернетка, ще не прийнята";
export const DASHBOARD_NOT_MATERIALIZED_NOTE = "Місяці й переплата з'являться, коли план буде зафіксовано.";
export const DASHBOARD_COMPARISON_MONTH_START_NOTE =
  "Цей план розраховано від фактичних залишків на початок місяця дії, тому порівняння стратегій тут не показується.";
export const DASHBOARD_COMPARISON_OTHER_DRAFT_NOTE = "Порівняння стратегій для цього плану тут не показується.";

export const ACCEPTANCE_BALANCES_CAPTION = "за залишками на момент прийняття";

// Що показує Dashboard у блоці плану. active — GET /debt/plan/active (null — прийнятого плану
// немає), draft — GET /debt/plan/latest (null — чернетки немає). Якщо є прийнятий план,
// показується чинна версія (effective, а без неї — прийнята), інакше остання чернетка.
// Порівняння стратегій для прийнятого плану — comparison версії (є лише в першої версії,
// start_kind=acceptance_balances) з підписом «за залишками на момент прийняття»; для версії від
// залишків на початок місяця дії порівняння немає — замість нього пояснення.
// Повертає null або { kind: "accepted" | "draft", caption, pendingText, materialized,
// monthsToPayoff, totalInterest, horizonMonths, comparison, comparisonCaption, comparisonNote }.
export function dashboardPlanView(active, draft) {
  if (active) {
    const shown = active.effective ?? active;
    const pending = active.pending && active.pending.version_id !== shown.version_id ? active.pending : null;
    const comparison = shown.start_kind === "acceptance_balances" ? shown.comparison ?? null : null;
    let comparisonNote = null;
    if (!comparison) {
      comparisonNote =
        shown.start_kind === "month_start_actual"
          ? DASHBOARD_COMPARISON_MONTH_START_NOTE
          : DASHBOARD_COMPARISON_OTHER_DRAFT_NOTE;
    }
    return {
      kind: "accepted",
      caption: `Прийнятий план, діє з ${formatDateUk(shown.effective_from)}`,
      pendingText: pending ? pendingPlanText(pending) : null,
      materialized: Boolean(shown.materialized),
      monthsToPayoff: shown.months_to_payoff ?? null,
      totalInterest: shown.total_interest ?? null,
      horizonMonths: null,
      comparison,
      comparisonCaption: comparison ? ACCEPTANCE_BALANCES_CAPTION : null,
      comparisonNote,
    };
  }
  if (draft) {
    return {
      kind: "draft",
      caption: DASHBOARD_DRAFT_CAPTION,
      pendingText: null,
      materialized: true,
      monthsToPayoff: draft.months_to_payoff ?? null,
      totalInterest: draft.total_interest_paid ?? null,
      horizonMonths: draft.horizon_months ?? null,
      comparison: draft.comparison ?? null,
      comparisonCaption: null,
      comparisonNote: null,
    };
  }
  return null;
}

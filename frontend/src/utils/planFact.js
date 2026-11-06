// Чисті функції блоку «План і факт» на сторінці «Борги»: суми, дати, місяці, тексти помилок.
// Місяці з API — рядки "YYYY-MM", дати — "YYYY-MM-DD"; порівнюються як рядки, без Date,
// щоб часовий пояс браузера не зсував день. Суми з API — гривні, показуються через formatUah.

import { formatDateUk } from "./debtPlan.js";
import { formatUah } from "./money.js";

// Як DEBT_BALANCE_MAX_UAH на сервері.
export const MONEY_MAX_UAH = 1_000_000_000;

// Слова, яких не має бути в жодному реченні блоку: різниця описується, а не оцінюється.
export const FORBIDDEN_WORDS = ["недоплат", "простроч", "чудово", "добре", "погано", "штраф"];

const MONTHS_NOMINATIVE = [
  "Січень", "Лютий", "Березень", "Квітень", "Травень", "Червень",
  "Липень", "Серпень", "Вересень", "Жовтень", "Листопад", "Грудень",
];

// Рядок з поля суми -> { value, error }. Ціле або з копійками ("500", "500,5", "500.50").
// allowZero — для залишку за випискою (0 — борг закрито); платіж має бути більшим за 0.
export function parseMoneyInput(raw, { allowZero = false } = {}) {
  const text = String(raw ?? "").trim().replace(/\s/g, "");
  if (text === "") return { value: null, error: "Вкажи суму." };
  if (text.startsWith("-")) return { value: null, error: "Сума не може бути від'ємною." };
  if (!/^\d+([.,]\d{1,2})?$/.test(text)) {
    return { value: null, error: "Введи число: ціле або з копійками (напр. 500 або 500,50)." };
  }
  const value = Number(text.replace(",", "."));
  if (!allowZero && value <= 0) return { value: null, error: "Сума має бути більшою за 0." };
  if (value > MONEY_MAX_UAH) return { value: null, error: "Сума завелика." };
  return { value, error: null };
}

// Речення наслідку (GET /debt/plan-fact/outcome): готовий текст сервера; null — наслідку немає.
export function outcomeText(outcome) {
  return typeof outcome?.sentence === "string" && outcome.sentence.trim() ? outcome.sentence : null;
}

// "YYYY-MM" -> назва місяця з великої літери і рік, напр. "Листопад YYYY".
export function formatMonthUk(month) {
  const match = /^(\d{4})-(\d{2})$/.exec(String(month ?? ""));
  if (!match || Number(match[2]) < 1 || Number(match[2]) > 12) return String(month ?? "—");
  return `${MONTHS_NOMINATIVE[Number(match[2]) - 1]} ${match[1]}`;
}

// Місяць для вибору за замовчуванням: поточний (today — серверна дата за Києвом, тож межа місяця
// за Києвом), якщо він є серед місяців, інакше останній.
export function defaultMonth(months, today) {
  if (!months?.length) return null;
  const current = today.slice(0, 7);
  return months.some((m) => m.month === current) ? current : months[months.length - 1].month;
}

// --- тексти помилок ---------------------------------------------------------------------------

export const PAYMENT_BEFORE_PLAN_TEXT = "Платіж раніше за дату прийняття плану, тому не враховується.";

const FIELD_ERROR_TEXTS = {
  amount: "Сума має бути більшою за 0.",
  balance: "Залишок не може бути від'ємним або завеликим.",
  paid_on: "Вкажи правильну дату.",
  as_of: "Вкажи правильну дату.",
  debt_id: "Вибери борг.",
  confirmed: "Не вдалося змінити позначку.",
};

// Помилка валідації FastAPI (масив { loc, msg }) -> речення для кожного поля, без технічних деталей.
function validationText(detail) {
  const texts = [];
  for (const item of detail) {
    const field = Array.isArray(item?.loc) ? item.loc[item.loc.length - 1] : null;
    const text = FIELD_ERROR_TEXTS[field] ?? "Перевір введені дані.";
    if (!texts.includes(text)) texts.push(text);
  }
  return texts.join(" ");
}

// Текст помилки запиту: рядок detail — як є (сервер пише простою мовою), масив валідації —
// речення за полями, інакше fallback із кодом.
export function serverErrorText(status, detail, fallback) {
  if (typeof detail === "string" && detail) return detail;
  if (Array.isArray(detail) && detail.length) return validationText(detail);
  return `${fallback}${status ? ` (HTTP ${status})` : ""}.`;
}

// Помилка внесення чи зміни платежу. 409 «дата раніше прийняття першого плану» — окремим реченням.
export function paymentErrorText(status, detail) {
  if (status === 409 && typeof detail === "string" && detail.includes("раніше дати прийняття")) {
    return PAYMENT_BEFORE_PLAN_TEXT;
  }
  return serverErrorText(status, detail, "Не вдалося зберегти платіж");
}


// --- місяць план-факту (GET /debt/plan-fact) ----------------------------------------------------

export const NOT_ENTERED_NOTE =
  "Фактичні залишки стануть відомі після внесення платежів або звірки з випискою.";

// Сума, яку вважаємо нульовою різницею (менше копійки).
function isZero(value) {
  return Math.round(Math.abs(value) * 100) === 0;
}

// Підписи стану місяця: "триває" (closed=false), "закрито", "платежів не внесено"
// (entry_state=not_entered), "позначено: платежів не було" (no_payments_confirmed).
// Закритий місяць без платежів має лише "платежів не внесено".
export function monthStateLabels(month) {
  const labels = [];
  const notEntered = month.entry_state === "not_entered";
  if (!month.closed) labels.push("триває");
  else if (!notEntered) labels.push("закрито");
  if (notEntered) labels.push("платежів не внесено");
  if (month.entry_state === "no_payments_confirmed") labels.push("позначено: платежів не було");
  return labels;
}

// "Враховано платежі з 4 листопада 2026" — лише для першого місяця першого плану.
export function countedFromText(month) {
  return month.counted_from ? `Враховано платежі з ${formatDateUk(month.counted_from)}` : null;
}

// Різниця платежу (факт − план) описово; null — різниця невідома.
export function paymentDiffText(diff) {
  if (diff === null || diff === undefined) return null;
  if (isZero(diff)) return "внесено стільки, скільки в плані";
  return diff < 0 ? `внесено на ${formatUah(-diff)} менше за план` : `внесено на ${formatUah(diff)} більше за план`;
}

// Різниця залишку (факт − план) описово; null — невідома.
export function balanceDiffText(diff) {
  if (diff === null || diff === undefined) return null;
  if (isZero(diff)) return "залишок такий, як у плані";
  return diff > 0
    ? `залишок на ${formatUah(diff)} більший за плановий`
    : `залишок на ${formatUah(-diff)} менший за плановий`;
}

export function remainingToPlanText(remaining) {
  return `Ще лишилося внести за планом: ${formatUah(remaining ?? 0)}`;
}

export const NO_DATA_TEXT = "немає даних";
export const CHECK_BALANCE_NOTE =
  "Після звірки відсотки решти місяця не враховано, тому залишок не порівнюється з плановим";

// Рядок боргу (або totals) для показу. Для місяця, що триває, замість різниці — скільки ще
// лишилося внести за планом (remaining_to_plan), а не різниця з планом.
// Повертає { planned, actual, diff, plannedClosing, actualClosing, balance } — готові рядки.
// actualClosing — «немає даних», коли фактичний залишок невідомий (платежів не внесено).
// balance — другорядне речення про залишок: у рядку зі звіркою (check) — CHECK_BALANCE_NOTE
// замість різниці залишків, у закритому місяці — різниця залишків, інакше null.
export function planFactRowView(row, month) {
  const inPlan = row.planned_payment !== null && row.planned_payment !== undefined;
  let diff;
  if (!inPlan) diff = "немає в плані на цей місяць";
  else if (!month.closed) diff = remainingToPlanText(row.remaining_to_plan);
  else diff = paymentDiffText(row.payment_diff) ?? "—";
  return {
    planned: inPlan ? formatUah(row.planned_payment) : "—",
    actual: formatUah(row.actual_payment),
    diff,
    plannedClosing: formatUah(row.planned_closing),
    actualClosing: row.actual_closing === null || row.actual_closing === undefined
      ? NO_DATA_TEXT
      : formatUah(row.actual_closing),
    balance: row.check ? CHECK_BALANCE_NOTE : month.closed ? balanceDiffText(row.balance_diff) : null,
  };
}

// Підсумок по закритих місяцях (cumulative з API, сервер рахує лише закриті місяці).
// null — закритих місяців ще немає.
export function cumulativeView(cumulative) {
  if (!cumulative?.debts?.length) return null;
  return {
    payment: paymentDiffText(cumulative.payment_diff),
    balance: balanceDiffText(cumulative.balance_diff),
    debts: cumulative.debts.map((d) => ({
      debt_id: d.debt_id,
      name: d.name,
      payment: paymentDiffText(d.payment_diff),
      balance: balanceDiffText(d.balance_diff),
    })),
  };
}

// --- позначка «платежів не було» ------------------------------------------------------------

export const NO_PAYMENTS_BUTTON_LABEL = "Платежів не було";
export const NO_PAYMENTS_UNDO_LABEL = "Скасувати позначку «платежів не було»";

export const MONTH_ONGOING_TEXT = "Місяць ще триває";

// Яку дію з позначкою показати для місяця, який можна змінювати:
// "unmark" — позначку вже поставлено (no_payments_mark), зокрема на місяці, що ще триває;
// "mark" — закритий місяць без платежів; "ongoing" — місяць без платежів ще триває, позначити
// не можна (замість кнопки — MONTH_ONGOING_TEXT); null — жодної.
export function noPaymentsAction(month) {
  if (!month?.editable) return null;
  const marked = month.no_payments_mark ?? month.entry_state === "no_payments_confirmed";
  if (marked) return "unmark";
  if (month.entry_state !== "not_entered") return null;
  return month.closed ? "mark" : "ongoing";
}

// --- незаповнений місяць у відповіді фіксації ---------------------------------------------------

const MONTHS_NOMINATIVE_LOWER = MONTHS_NOMINATIVE.map((name) => name.toLowerCase());

// "YYYY-MM" -> "листопад 2026 р." (як місяць у реченнях сервера); null — не місяць.
export function monthNameUk(month) {
  const match = /^(\d{4})-(\d{2})$/.exec(String(month ?? ""));
  if (!match || Number(match[2]) < 1 || Number(match[2]) > 12) return null;
  return `${MONTHS_NOMINATIVE_LOWER[Number(match[2]) - 1]} ${match[1]} р.`;
}

// Місяць, який треба заповнити, щоб зафіксувати план: поле unresolved_month відповіді 409
// POST /debt/plan/materialize; null — інша причина відмови.
export function materializeUnresolvedMonth(status, data) {
  if (status !== 409) return null;
  const month = data?.unresolved_month;
  return monthNameUk(month) ? month : null;
}

// Підписи дій під повідомленням про незаповнений місяць; null — місяця немає.
export function unresolvedMonthLabels(month) {
  const name = monthNameUk(month);
  if (!name) return null;
  return {
    mark: `Позначити, що платежів за ${name} не було`,
    enter: `Внести платежі за ${name}`,
  };
}

// Місяць для селектора після запиту перейти до місяця requested: requested, якщо він є
// серед місяців, інакше поточний вибір current.
export function focusedMonth(months, requested, current) {
  return requested && months?.some((m) => m.month === requested) ? requested : current;
}

// --- звірка залишку з випискою ---------------------------------------------------------------

export const BALANCE_CHECK_LABEL = "Звірити залишок з випискою";
export const BALANCE_CHECK_NOTE = "Від цієї дати залишок рахується за випискою.";

// Дата звірки за замовчуванням: сьогодні для поточного місяця, інакше останній день місяця.
export function defaultCheckDate(month, today) {
  if (month === today.slice(0, 7)) return today;
  const [y, m] = month.split("-").map(Number);
  const lastDay = new Date(Date.UTC(y, m, 0)).getUTCDate();
  const value = `${month}-${String(lastDay).padStart(2, "0")}`;
  return value > today ? today : value;
}

// Підпис наявної звірки в рядку боргу; null — звірки в місяці немає.
export function checkCaption(check) {
  if (!check) return null;
  return `Звірка з випискою: ${formatUah(check.balance)} на ${formatDateUk(check.as_of)}`;
}

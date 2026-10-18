// Чисті функції сторінки Transactions.jsx.

// Групи категорій з GET /transactions/categories, які можна обрати в
// select. "custom" — власні категорії витрат користувача (custom_categories;
// бекенд кладе їх і в expense, тут вони виділені в окрему групу).
// "service" (pending, "Повернення позики") — лише для відображення.
export const EDITABLE_GROUPS = ["expense", "custom", "income", "transfer"];

export const GROUP_LABELS = {
  expense: "Витрати",
  custom: "Власні",
  income: "Дохід",
  transfer: "Перекази",
  service: "Службові",
};

// Відповідь /transactions/categories -> групи для select: власні окремо від
// вбудованих витрат.
export function displayGroups(categories) {
  const custom = categories.custom_categories || [];
  return {
    ...categories,
    expense: (categories.expense || []).filter((c) => !custom.includes(c)),
    custom,
  };
}

// Ті самі правила, що в бекенді (mcc_mapping.validate_manual_category_sign):
// додатна сума — дохід або переказ, від'ємна — витрата (вбудована чи власна)
// або переказ; для нуля обмежень немає. Остаточна перевірка — на бекенді (422).
export function allowedGroupsForAmount(amountKopecks) {
  if (amountKopecks > 0) return ["income", "transfer"];
  if (amountKopecks < 0) return ["expense", "custom", "transfer"];
  return [...EDITABLE_GROUPS];
}

// Копійки -> "1 003,57 грн" (зі знаком "+"/"−" для ненульових сум).
// Цілочисельно, без amount / 100 у float — щоб не було 1003.5699999.
const UAH_GROUPING = new Intl.NumberFormat("uk-UA", { maximumFractionDigits: 0 });

export function formatAmountKopecks(amountKopecks, { withSign = true } = {}) {
  if (amountKopecks === null || amountKopecks === undefined) return "—";
  const abs = Math.abs(amountKopecks);
  const whole = UAH_GROUPING.format(Math.floor(abs / 100));
  const cents = String(abs % 100).padStart(2, "0");
  const sign = !withSign || amountKopecks === 0 ? "" : amountKopecks > 0 ? "+" : "−";
  return `${sign}${whole},${cents} грн`;
}

// Unix-секунди -> "ДД.ММ.РРРР" за Києвом (ті самі календарні дні, що й
// фільтр from/to на бекенді), незалежно від timezone браузера.
const KYIV_DATE_PARTS = new Intl.DateTimeFormat("uk-UA", {
  timeZone: "Europe/Kyiv",
  day: "2-digit",
  month: "2-digit",
  year: "numeric",
});

export function formatKyivDate(unixSeconds) {
  if (unixSeconds === null || unixSeconds === undefined) return "—";
  const parts = Object.fromEntries(
    KYIV_DATE_PARTS.formatToParts(new Date(unixSeconds * 1000)).map((p) => [p.type, p.value])
  );
  return `${parts.day}.${parts.month}.${parts.year}`;
}

export const METHOD_LABELS = {
  mcc: "MCC",
  keyword: "ключове слово",
  ml: "ML",
  manual: "вручну",
  bank_provided: "від банку",
  reimbursement: "повернення витрати",
  pending: "без категорії",
  income_backfill_fallback: "backfill",
};

// Текст помилки з відповіді axios: detail з HTTPException — рядок,
// з валідації FastAPI — масив об'єктів {msg}.
export function errorDetail(error, fallback) {
  if (!error?.response) return "Немає зв'язку з сервером — спробуй ще раз.";
  const detail = error.response.data?.detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) return detail.map((d) => d.msg).filter(Boolean).join("; ") || fallback;
  return fallback;
}

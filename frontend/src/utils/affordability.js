// Чисті функції калькулятора спроможності на сторінці «Борги».

export const EXTRA_MAX_UAH = 10_000_000;

// Рядок з поля -> { value: number | null, error: string | null }.
// Приймає ціле або з копійками ("5000", "5000.5", "5000,50"); NaN, Infinity,
// експоненту ("1e5"), від'ємні й > 10 000 000 відхиляє — тоді запити не йдуть.
export function parseExtraInput(raw) {
  const text = String(raw ?? "").trim().replace(/\s/g, "");
  if (text === "") return { value: null, error: "Вкажи суму (0 — лише мінімальні платежі)." };
  if (text.startsWith("-")) return { value: null, error: "Сума не може бути від'ємною." };
  if (!/^\d+([.,]\d{1,2})?$/.test(text)) {
    return { value: null, error: "Введи число: ціле або з копійками (напр. 5000 або 5000,50)." };
  }
  const value = Number(text.replace(",", "."));
  if (!Number.isFinite(value)) return { value: null, error: "Введи скінченне число." };
  if (value > EXTRA_MAX_UAH) return { value: null, error: "Не більше 10 000 000 грн." };
  return { value, error: null };
}

const UAH = new Intl.NumberFormat("uk-UA", { maximumFractionDigits: 0 });

export function formatUah(value) {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  return `${value < 0 ? "−" : ""}${UAH.format(Math.abs(value))} грн`;
}

// "YYYY-MM" -> "MM.YYYY"
export function formatMonth(month) {
  const [y, m] = String(month).split("-");
  return m && y ? `${m}.${y}` : String(month);
}

// Текст помилки з відповіді axios: рядок detail як є; масив валідації — msg через "; ".
export function responseDetail(error) {
  const detail = error?.response?.data?.detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) return detail.map((d) => d.msg).filter(Boolean).join("; ");
  return null;
}

// Єдиний формат сум в інтерфейсі: цілі гривні з округленням half-up за модулем і роздільниками
// розрядів uk-UA; суму, меншу за 1 грн за модулем (і не нуль), — з двома знаками після коми.

const UAH_WHOLE = new Intl.NumberFormat("uk-UA", { maximumFractionDigits: 0 });
const UAH_FRACTION = new Intl.NumberFormat("uk-UA", { minimumFractionDigits: 2, maximumFractionDigits: 2 });

// Гривні -> "12 346 грн", "0,40 грн", "−500 грн"; null, undefined, NaN, ±Infinity -> "—".
// withSign — "+" перед додатною сумою (для надходжень).
export function formatUah(value, { withSign = false } = {}) {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  const kop = Math.round(Math.abs(value) * 100);
  if (kop === 0) return "0 грн";
  const text = kop < 100 ? UAH_FRACTION.format(kop / 100) : UAH_WHOLE.format(Math.round(kop / 100));
  const sign = value < 0 ? "−" : withSign ? "+" : "";
  return `${sign}${text} грн`;
}

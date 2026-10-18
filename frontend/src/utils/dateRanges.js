/**
 * Часові діапазони для аналітики за категоріями.
 * Усі діапазони закінчуються останнім повним місяцем; поточний неповний
 * місяць не входить.
 */

export function getLastFullMonthRange() {
  const now = new Date();
  const startOfCurrentMonth = new Date(now.getFullYear(), now.getMonth(), 1);
  const end = new Date(startOfCurrentMonth.getTime() - 1); // останній момент попереднього місяця
  const start = new Date(end.getFullYear(), end.getMonth(), 1);
  return { start, end };
}

function monthsBackFromLastFullMonth(monthsBack) {
  const { end } = getLastFullMonthRange();
  const start = new Date(end.getFullYear(), end.getMonth() - monthsBack + 1, 1);
  return { start, end };
}

export function monthsInRange(start, end) {
  return (end.getFullYear() - start.getFullYear()) * 12 + (end.getMonth() - start.getMonth()) + 1;
}

export const PERIOD_OPTIONS = [
  { id: "quarter", label: "Квартал" },
  { id: "half_year", label: "Півроку" },
  { id: "year", label: "Рік" },
  { id: "ytd", label: "З початку року" },
  { id: "last_year", label: "Минулий рік" },
  { id: "all_time", label: "Весь період" },
];

export function getPeriodRange(periodId, earliestTransactionDate) {
  const { end: lastFullMonthEnd } = getLastFullMonthRange();

  switch (periodId) {
    case "quarter":
      return monthsBackFromLastFullMonth(3);
    case "half_year":
      return monthsBackFromLastFullMonth(6);
    case "year":
      return monthsBackFromLastFullMonth(12);
    case "ytd": {
      const start = new Date(lastFullMonthEnd.getFullYear(), 0, 1);
      return { start, end: lastFullMonthEnd };
    }
    case "last_year": {
      const prevYear = lastFullMonthEnd.getFullYear() - 1;
      return { start: new Date(prevYear, 0, 1), end: new Date(prevYear, 11, 31, 23, 59, 59) };
    }
    case "all_time":
    default:
      return { start: earliestTransactionDate || new Date(2000, 0, 1), end: lastFullMonthEnd };
  }
}

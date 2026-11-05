// Чисті функції для полів із цілим числом: стан поля — рядок, щоб під час набору його можна
// було очистити, а початкові нулі не лишалися.

// Під час набору: лише цифри, без початкових нулів; порожній рядок дозволено ("012" -> "12",
// "0" -> "0", "" -> "").
export function normalizeIntegerInput(raw) {
  const digits = String(raw ?? "").replace(/\D/g, "");
  if (digits === "") return "";
  const trimmed = digits.replace(/^0+/, "");
  return trimmed === "" ? "0" : trimmed;
}

// Під час втрати фокуса чи надсилання: число в межах [min, max]; порожнє або нечислове — fallback.
export function clampInteger(raw, min, max, fallback) {
  const text = normalizeIntegerInput(raw);
  if (text === "") return fallback;
  return Math.min(max, Math.max(min, Number(text)));
}

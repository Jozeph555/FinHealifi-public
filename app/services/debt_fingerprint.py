"""
Відбиток складу активних боргів для збереженого плану.

POST /debt/optimize зберігає відбиток боргів, з яких побудовано план;
GET /debt/plan/latest порівнює його з поточним і повертає is_stale.

У відбиток входять id, тип і числові параметри, які використовує MILP.
Назва боргу не входить: MILP використовує її лише як ключ і підпис.
Порядок боргів не впливає на результат (сортування за id), числа
приводяться до float, тож 1000 і 1000.0 дають той самий відбиток.
"""
import hashlib
import json

FINGERPRINT_FIELDS = ("id", "debt_type", "balance", "monthly_rate", "min_payment")
_NUMERIC_FIELDS = {"balance", "monthly_rate", "min_payment"}


def _normalize(field: str, value):
    """Приводить числові поля до float."""
    if field in _NUMERIC_FIELDS and value is not None:
        return float(value)
    return value


def debts_fingerprint(debts) -> str:
    """Рахує відбиток списку боргів.

    Args:
        debts: Активні борги користувача — об'єкти з атрибутами FINGERPRINT_FIELDS.

    Returns:
        sha256 у hex-форматі від відсортованого за id списку параметрів боргів.
    """
    rows = sorted(
        ([_normalize(f, getattr(d, f)) for f in FINGERPRINT_FIELDS] for d in debts),
        key=lambda row: row[0],
    )
    payload = json.dumps({"fields": FINGERPRINT_FIELDS, "debts": rows}, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()

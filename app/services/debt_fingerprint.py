"""
Відбиток складу активних боргів для збереженого плану.

POST /debt/optimize зберігає відбиток боргів, з яких побудовано план;
draft_changes порівнює відбитки чернетки з поточними боргами — це правило
читають і GET /debt/plan/latest (balances_changed, structure_changed), і
прийняття чернетки.

У відбиток входять id, тип і числові параметри, які використовує MILP.
Назва боргу не входить: MILP використовує її лише як ключ і підпис.
Порядок боргів не впливає на результат (сортування за id), числа
приводяться до float, тож 1000 і 1000.0 дають той самий відбиток.

debts_structure_fingerprint — відбиток складу без залишків: його використовують
прийняті версії плану, яким зміна залишку від платежів не заважає.
"""
import hashlib
import json

FINGERPRINT_FIELDS = ("id", "debt_type", "balance", "monthly_rate", "min_payment")
STRUCTURE_FIELDS = ("id", "debt_type", "monthly_rate", "min_payment")
_NUMERIC_FIELDS = {"balance", "monthly_rate", "min_payment"}


def _normalize(field: str, value):
    """Приводить числові поля до float."""
    if field in _NUMERIC_FIELDS and value is not None:
        return float(value)
    return value


def _fingerprint(debts, fields: tuple) -> str:
    """sha256 від відсортованого за id списку значень fields."""
    rows = sorted(
        ([_normalize(f, getattr(d, f)) for f in fields] for d in debts),
        key=lambda row: row[0],
    )
    payload = json.dumps({"fields": fields, "debts": rows}, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def debts_fingerprint(debts) -> str:
    """Рахує відбиток списку боргів.

    Args:
        debts: Активні борги користувача — об'єкти з атрибутами FINGERPRINT_FIELDS;
            balance — поточний залишок з журналу (debt_ledger).

    Returns:
        sha256 у hex-форматі від відсортованого за id списку параметрів боргів.
    """
    return _fingerprint(debts, FINGERPRINT_FIELDS)


def debts_structure_fingerprint(debts) -> str:
    """Рахує відбиток складу боргів без залишків (поля STRUCTURE_FIELDS).

    Returns:
        sha256 у hex-форматі.
    """
    return _fingerprint(debts, STRUCTURE_FIELDS)


def draft_changes(plan, debts) -> tuple[bool | None, bool | None]:
    """Порівнює відбитки чернетки з поточними активними боргами.

    Args:
        plan: Чернетка з debts_fingerprint і debts_structure_fingerprint.
        debts: Поточні активні борги (об'єкти з атрибутами FINGERPRINT_FIELDS).

    Returns:
        Пара (balances_changed, structure_changed). structure_changed — змінився
        склад активних боргів, тип, ставка чи мінімальний платіж;
        balances_changed — за того самого складу змінилися лише залишки.
        Без повного відбитка — (None, None); без відбитка складу будь-яка
        зміна вважається зміною складу.
    """
    if plan.debts_fingerprint is None:
        return None, None
    any_change = debts_fingerprint(debts) != plan.debts_fingerprint
    if plan.debts_structure_fingerprint is None:
        return False, any_change
    structure_changed = debts_structure_fingerprint(debts) != plan.debts_structure_fingerprint
    return any_change and not structure_changed, structure_changed

import { useEffect, useRef, useState } from "react";
import api from "../services/api";
import AmountRow, { RowAction } from "./ui/AmountRow";
import { formatDateUk } from "../utils/debtPlan";
import { formatUah } from "../utils/money";
import {
  BALANCE_CHECK_LABEL,
  BALANCE_CHECK_NOTE,
  NOT_ENTERED_NOTE,
  NO_DATA_TEXT,
  NO_PAYMENTS_BUTTON_LABEL,
  NO_PAYMENTS_UNDO_LABEL,
  MONTH_ONGOING_TEXT,
  checkCaption,
  countedFromText,
  cumulativeView,
  defaultCheckDate,
  defaultMonth,
  focusedMonth,
  formatMonthUk,
  monthStateLabels,
  noPaymentsAction,
  outcomeText,
  parseMoneyInput,
  paymentErrorText,
  planFactRowView,
  serverErrorText,
} from "../utils/planFact";

const INPUT_CLASS =
  "w-full rounded-lg border border-ink/15 bg-white px-3 py-2 outline-none focus:border-forest focus:ring-1 focus:ring-forest";

// Блок «План і факт» під карткою плану: платежі за боргами, порівняння з планом по місяцях.
// debts — активні борги (GET /debt); today — сьогодні за Києвом, "YYYY-MM-DD";
// onLedgerChange — викликається після кожного запису (платіж, звірка, позначка місяця),
// щоб сторінка перечитала залишки боргів. focusRequest — { month: "YYYY-MM", nonce }: перемкнути
// селектор на цей місяць і прокрутити до блоку; reloadToken — зміна значення перечитує план-факт.
export default function PlanFact({ debts, today, onLedgerChange, focusRequest, reloadToken }) {
  const [planFact, setPlanFact] = useState(null);
  const [loadError, setLoadError] = useState(null);
  const [month, setMonth] = useState(null);
  const [payments, setPayments] = useState([]);
  const [paymentsError, setPaymentsError] = useState(null);
  // Наслідок внесених платежів (GET /debt/plan-fact/outcome); null — немає або не завантажено.
  const [outcome, setOutcome] = useState(null);

  async function loadOutcome() {
    try {
      const res = await api.get("/debt/plan-fact/outcome");
      setOutcome(res.data);
    } catch {
      setOutcome(null);
    }
  }

  async function loadPlanFact() {
    loadOutcome();
    try {
      const res = await api.get("/debt/plan-fact");
      setPlanFact(res.data);
      setLoadError(null);
      setMonth((current) =>
        current && res.data.months.some((m) => m.month === current) ? current : defaultMonth(res.data.months, today)
      );
    } catch (err) {
      setPlanFact(null);
      setLoadError(
        err.response?.status === 404
          ? "Порівняння з'явиться, коли план буде прийнято й зафіксовано."
          : serverErrorText(err.response?.status, err.response?.data?.detail, "Не вдалося завантажити план і факт")
      );
    }
  }

  async function loadPayments(selected) {
    if (!selected) return;
    try {
      const res = await api.get("/debt/payments", { params: { month: selected } });
      setPayments(res.data);
      setPaymentsError(null);
    } catch (err) {
      setPayments([]);
      setPaymentsError(serverErrorText(err.response?.status, err.response?.data?.detail, "Не вдалося завантажити платежі"));
    }
  }

  useEffect(() => {
    loadPlanFact();
  }, [reloadToken]);

  // Запит перейти до місяця виконується один раз, коли місяці вже завантажено.
  const sectionRef = useRef(null);
  const handledFocus = useRef(null);
  useEffect(() => {
    if (!focusRequest || !planFact || handledFocus.current === focusRequest.nonce) return;
    handledFocus.current = focusRequest.nonce;
    setMonth((current) => focusedMonth(planFact.months, focusRequest.month, current));
    sectionRef.current?.scrollIntoView?.({ behavior: "smooth", block: "start" });
  }, [focusRequest, planFact]);

  useEffect(() => {
    loadPayments(month);
  }, [month]);

  // Після будь-якого запису: план-факт, платежі місяця і залишки на сторінці.
  async function refresh() {
    await Promise.all([loadPlanFact(), loadPayments(month)]);
    onLedgerChange?.();
  }

  const months = planFact?.months ?? [];
  const selected = months.find((m) => m.month === month) ?? null;
  const names = Object.fromEntries([
    ...months.flatMap((m) => m.debts.map((d) => [d.debt_id, d.name])),
    ...debts.map((d) => [d.id, d.name]),
  ]);

  return (
    <section ref={sectionRef} className="rounded-xl border border-ink/10 bg-white p-5">
      <h2 className="mb-4 text-sm font-medium uppercase tracking-wide text-ink/50">План і факт</h2>

      <div className="space-y-5">
        <PaymentForm debts={debts} today={today} onSaved={refresh} />

        {loadError && <p className="text-sm text-ink/60">{loadError}</p>}

        {selected && (
          <div className="space-y-4">
            <div className="flex flex-wrap items-center gap-2">
              <label className="text-sm text-ink/70" htmlFor="plan-fact-month">
                Місяць
              </label>
              <select
                id="plan-fact-month"
                value={month}
                onChange={(e) => setMonth(e.target.value)}
                className="rounded-lg border border-ink/15 bg-white px-3 py-1.5 text-sm outline-none focus:border-forest"
              >
                {months.map((m) => (
                  <option key={m.month} value={m.month}>
                    {formatMonthUk(m.month)}
                  </option>
                ))}
              </select>
            </div>

            <MonthComparison month={selected} today={today} onChanged={refresh} />

            {outcomeText(outcome) && (
              <p className="rounded-lg bg-ink/5 px-3 py-2 text-sm text-ink/80">{outcomeText(outcome)}</p>
            )}

            <NoPaymentsMark month={selected} onChanged={refresh} />

            <PaymentList
              payments={payments}
              names={names}
              editable={selected.editable}
              today={today}
              error={paymentsError}
              onChanged={refresh}
            />
          </div>
        )}

        {planFact && <CumulativeSummary cumulative={planFact.cumulative} />}
      </div>
    </section>
  );
}

// Позначка «платежів не було» (PUT /debt/plan-fact/months/{YYYY-MM}/no-payments) і її скасування.
function NoPaymentsMark({ month, onChanged }) {
  const [error, setError] = useState(null);
  const [isSaving, setIsSaving] = useState(false);
  const action = noPaymentsAction(month);

  useEffect(() => setError(null), [month.month]);

  async function handleClick() {
    setIsSaving(true);
    setError(null);
    try {
      await api.put(`/debt/plan-fact/months/${month.month}/no-payments`, { confirmed: action === "mark" });
      await onChanged();
    } catch (err) {
      setError(serverErrorText(err.response?.status, err.response?.data?.detail, "Не вдалося змінити позначку"));
    } finally {
      setIsSaving(false);
    }
  }

  if (!action && !error) return null;
  if (action === "ongoing") return <p className="text-xs text-ink/50">{MONTH_ONGOING_TEXT}</p>;
  return (
    <div className="space-y-2">
      {action && (
        <button
          onClick={handleClick}
          disabled={isSaving}
          className="rounded-lg border border-forest/40 px-3 py-1.5 text-xs font-medium text-forest transition hover:bg-forest/5 disabled:opacity-50"
        >
          {action === "mark" ? NO_PAYMENTS_BUTTON_LABEL : NO_PAYMENTS_UNDO_LABEL}
        </button>
      )}
      {error && <p className="rounded-lg bg-brick/10 px-3 py-2 text-sm text-brick">{error}</p>}
    </div>
  );
}

// Порівняння вибраного місяця: стан, рядки боргів і «Разом».
function MonthComparison({ month, today, onChanged }) {
  const counted = countedFromText(month);
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap gap-2">
        {monthStateLabels(month).map((label) => (
          <span key={label} className="rounded-full bg-ink/5 px-2.5 py-0.5 text-xs text-ink/70">
            {label}
          </span>
        ))}
      </div>
      {counted && <p className="text-xs text-ink/60">{counted}</p>}
      {month.closed && month.entry_state === "not_entered" && (
        <p className="text-xs text-ink/60">{NOT_ENTERED_NOTE}</p>
      )}
      {month.debts.length === 0 ? (
        <p className="text-sm text-ink/60">У цьому місяці немає боргів у плані.</p>
      ) : (
        <div className="space-y-2">
          {month.debts.map((row) => (
            <ComparisonRow key={row.debt_id} title={row.name} row={row} month={month}>
              {checkCaption(row.check) && <p className="mt-2 text-xs text-ink/50">{checkCaption(row.check)}</p>}
              {month.editable && (
                <BalanceCheck debtId={row.debt_id} month={month.month} today={today} onSaved={onChanged} />
              )}
            </ComparisonRow>
          ))}
          {month.debts.length > 1 && <ComparisonRow title="Разом" row={month.totals} month={month} total />}
        </div>
      )}
    </div>
  );
}

// Один борг (або «Разом»): план, внесено, різниця / скільки лишилося, залишки на кінець місяця.
function ComparisonRow({ title, row, month, total = false, children }) {
  const view = planFactRowView(row, month);
  return (
    <div className={`rounded-lg px-3 py-2.5 ${total ? "bg-forest/5" : "border border-ink/10"}`}>
      <p className={`mb-2 text-sm ${total ? "font-medium text-forest" : "text-ink"}`}>{title}</p>
      <dl className="grid grid-cols-2 gap-x-4 gap-y-2 text-xs md:grid-cols-5">
        <Cell label="Плановий платіж" value={view.planned} mono />
        <Cell label="Внесено" value={view.actual} mono />
        <Cell label={month.closed ? "Різниця" : "За планом"} value={view.diff} wide />
        <Cell label="Плановий залишок на кінець місяця" value={view.plannedClosing} mono />
        <Cell label="Фактичний залишок" value={view.actualClosing} mono={view.actualClosing !== NO_DATA_TEXT} />
      </dl>
      {view.balance && <p className="mt-2 text-xs text-ink/60">{view.balance}</p>}
      {children}
    </div>
  );
}

// Звірка залишку з випискою: дата і залишок → POST /debt/{id}/balance-checks. Видалення звірок немає.
function BalanceCheck({ debtId, month, today, onSaved }) {
  const [isOpen, setIsOpen] = useState(false);
  const [asOf, setAsOf] = useState(() => defaultCheckDate(month, today));
  const [balance, setBalance] = useState("");
  const [error, setError] = useState(null);
  const [isSaving, setIsSaving] = useState(false);

  useEffect(() => {
    setAsOf(defaultCheckDate(month, today));
    setIsOpen(false);
    setError(null);
  }, [month, today]);

  async function handleSubmit(e) {
    e.preventDefault();
    if (!asOf) {
      setError("Вкажи дату з виписки.");
      return;
    }
    const parsed = parseMoneyInput(balance, { allowZero: true });
    if (parsed.error) {
      setError(parsed.error);
      return;
    }
    setIsSaving(true);
    setError(null);
    try {
      await api.post(`/debt/${debtId}/balance-checks`, { as_of: asOf, balance: parsed.value });
      setBalance("");
      setIsOpen(false);
      await onSaved();
    } catch (err) {
      setError(serverErrorText(err.response?.status, err.response?.data?.detail, "Не вдалося зберегти звірку"));
    } finally {
      setIsSaving(false);
    }
  }

  if (!isOpen) {
    return (
      <button onClick={() => setIsOpen(true)} className="mt-2 text-xs text-forest underline decoration-forest/30 underline-offset-2 hover:decoration-forest">
        {BALANCE_CHECK_LABEL}
      </button>
    );
  }
  return (
    <form onSubmit={handleSubmit} noValidate className="mt-2 space-y-2 rounded-lg bg-ink/5 p-3">
      <p className="text-xs text-ink/70">{BALANCE_CHECK_NOTE}</p>
      <div className="grid gap-2 sm:grid-cols-2">
        <input
          type="date"
          value={asOf}
          max={today}
          onChange={(e) => setAsOf(e.target.value)}
          className={INPUT_CLASS}
          aria-label="Дата з виписки"
        />
        <input
          type="text"
          inputMode="decimal"
          value={balance}
          onChange={(e) => setBalance(e.target.value)}
          placeholder="Залишок із виписки, грн"
          className={INPUT_CLASS}
          aria-label="Залишок із виписки, грн"
        />
      </div>
      {error && <p className="rounded-lg bg-brick/10 px-3 py-2 text-sm text-brick">{error}</p>}
      <div className="flex gap-2">
        <button
          type="submit"
          disabled={isSaving}
          className="rounded-lg bg-forest px-3 py-1.5 text-xs font-medium text-paper transition hover:bg-forest-light disabled:opacity-50"
        >
          {isSaving ? "Зберігаємо..." : "Зберегти звірку"}
        </button>
        <button
          type="button"
          onClick={() => setIsOpen(false)}
          className="rounded-lg border border-ink/15 px-3 py-1.5 text-xs font-medium text-ink/60 transition hover:bg-ink/5"
        >
          Скасувати
        </button>
      </div>
    </form>
  );
}

function Cell({ label, value, mono = false, wide = false }) {
  return (
    <div className={wide ? "col-span-2 md:col-span-1" : ""}>
      <dt className="text-ink/50">{label}</dt>
      <dd className={`text-ink ${mono ? "font-mono" : ""}`}>{value}</dd>
    </div>
  );
}

// Підсумок по закритих місяцях (cumulative з GET /debt/plan-fact).
function CumulativeSummary({ cumulative }) {
  const view = cumulativeView(cumulative);
  return (
    <div className="space-y-2 border-t border-ink/10 pt-4">
      <p className="text-xs text-ink/50">Разом за закриті місяці</p>
      {!view ? (
        <p className="text-sm text-ink/60">Закритих місяців ще немає.</p>
      ) : (
        <>
          <p className="text-sm text-ink/80">
            {[view.payment, view.balance].filter(Boolean).join("; ")}
          </p>
          {view.debts.length > 1 &&
            view.debts.map((d) => (
              <p key={d.debt_id} className="text-xs text-ink/60">
                {d.name}: {[d.payment, d.balance].filter(Boolean).join("; ")}
              </p>
            ))}
        </>
      )}
    </div>
  );
}

// Форма «Внести платіж»: борг, дата (сьогодні за Києвом), сума в гривнях. Плановою сумою не заповнюється.
function PaymentForm({ debts, today, onSaved }) {
  const [debtId, setDebtId] = useState("");
  const [paidOn, setPaidOn] = useState(today);
  const [amount, setAmount] = useState("");
  const [error, setError] = useState(null);
  const [notice, setNotice] = useState(null);
  const [isSaving, setIsSaving] = useState(false);
  const inFlight = useRef(false);

  async function handleSubmit(e) {
    e.preventDefault();
    if (inFlight.current) return;
    setNotice(null);
    if (!debtId) {
      setError("Вибери борг.");
      return;
    }
    if (!paidOn) {
      setError("Вкажи дату платежу.");
      return;
    }
    const parsed = parseMoneyInput(amount);
    if (parsed.error) {
      setError(parsed.error);
      return;
    }
    inFlight.current = true;
    setIsSaving(true);
    setError(null);
    try {
      await api.post("/debt/payments", { debt_id: debtId, paid_on: paidOn, amount: parsed.value });
      setAmount("");
      setNotice(`Платіж ${formatUah(parsed.value)} від ${formatDateUk(paidOn)} внесено.`);
      await onSaved();
    } catch (err) {
      setError(paymentErrorText(err.response?.status, err.response?.data?.detail));
    } finally {
      inFlight.current = false;
      setIsSaving(false);
    }
  }

  if (debts.length === 0) return null;

  return (
    <form onSubmit={handleSubmit} noValidate className="space-y-3 rounded-lg border border-ink/10 p-3">
      <p className="text-sm font-medium text-ink">Внести платіж</p>
      <div className="grid gap-3 sm:grid-cols-3">
        <div>
          <label className="mb-1 block text-xs text-ink/60" htmlFor="payment-debt">
            Борг
          </label>
          <select id="payment-debt" value={debtId} onChange={(e) => setDebtId(e.target.value)} className={INPUT_CLASS}>
            <option value="">— вибери борг —</option>
            {debts.map((d) => (
              <option key={d.id} value={d.id}>
                {d.name}
              </option>
            ))}
          </select>
        </div>
        <div>
          <label className="mb-1 block text-xs text-ink/60" htmlFor="payment-date">
            Дата
          </label>
          <input
            id="payment-date"
            type="date"
            value={paidOn}
            max={today}
            onChange={(e) => setPaidOn(e.target.value)}
            className={INPUT_CLASS}
          />
        </div>
        <div>
          <label className="mb-1 block text-xs text-ink/60" htmlFor="payment-amount">
            Сума, грн
          </label>
          <input
            id="payment-amount"
            type="text"
            inputMode="decimal"
            value={amount}
            onChange={(e) => setAmount(e.target.value)}
            placeholder="напр. 1500"
            className={INPUT_CLASS}
          />
        </div>
      </div>
      {error && <p className="rounded-lg bg-brick/10 px-3 py-2 text-sm text-brick">{error}</p>}
      {notice && <p className="text-sm text-sage">{notice}</p>}
      <button
        type="submit"
        disabled={isSaving}
        className="rounded-lg bg-forest px-4 py-2 text-sm font-medium text-paper transition hover:bg-forest-light disabled:opacity-50"
      >
        {isSaving ? "Зберігаємо..." : "Внести платіж"}
      </button>
    </form>
  );
}

// Платежі вибраного місяця; редагування й видалення — лише коли місяць можна змінювати.
function PaymentList({ payments, names, editable, today, error, onChanged }) {
  const [editingId, setEditingId] = useState(null);
  const [editDate, setEditDate] = useState("");
  const [editAmount, setEditAmount] = useState("");
  const [actionError, setActionError] = useState(null);
  const [isBusy, setIsBusy] = useState(false);

  function startEdit(p) {
    setEditingId(p.id);
    setEditDate(p.paid_on);
    setEditAmount(String(p.amount).replace(".", ","));
    setActionError(null);
  }

  async function handleSave(p) {
    const parsed = parseMoneyInput(editAmount);
    if (parsed.error) {
      setActionError(parsed.error);
      return;
    }
    if (!editDate) {
      setActionError("Вкажи дату платежу.");
      return;
    }
    setIsBusy(true);
    setActionError(null);
    try {
      await api.patch(`/debt/payments/${p.id}`, { paid_on: editDate, amount: parsed.value });
      setEditingId(null);
      await onChanged();
    } catch (err) {
      setActionError(paymentErrorText(err.response?.status, err.response?.data?.detail));
    } finally {
      setIsBusy(false);
    }
  }

  async function handleDelete(p) {
    if (!confirm(`Видалити платіж ${formatUah(p.amount)} від ${formatDateUk(p.paid_on)}?`)) return;
    setIsBusy(true);
    setActionError(null);
    try {
      await api.delete(`/debt/payments/${p.id}`);
      await onChanged();
    } catch (err) {
      setActionError(serverErrorText(err.response?.status, err.response?.data?.detail, "Не вдалося видалити платіж"));
    } finally {
      setIsBusy(false);
    }
  }

  return (
    <div className="space-y-2">
      <p className="text-xs text-ink/50">Платежі за місяць</p>
      {error && <p className="text-sm text-brick">{error}</p>}
      {!error && payments.length === 0 && <p className="text-sm text-ink/60">Платежів за цей місяць ще немає.</p>}
      {payments.map((p) =>
        editingId === p.id ? (
          <div key={p.id} className="space-y-2 rounded-lg border border-forest/30 bg-forest/5 p-3">
            <p className="text-sm text-ink">{names[p.debt_id] ?? "Борг"}</p>
            <div className="grid gap-2 sm:grid-cols-2">
              <input
                type="date"
                value={editDate}
                max={today}
                onChange={(e) => setEditDate(e.target.value)}
                className={INPUT_CLASS}
                aria-label="Дата платежу"
              />
              <input
                type="text"
                inputMode="decimal"
                value={editAmount}
                onChange={(e) => setEditAmount(e.target.value)}
                className={INPUT_CLASS}
                aria-label="Сума, грн"
              />
            </div>
            <div className="flex gap-2">
              <button
                onClick={() => handleSave(p)}
                disabled={isBusy}
                className="rounded-lg bg-forest px-3 py-1.5 text-xs font-medium text-paper transition hover:bg-forest-light disabled:opacity-50"
              >
                Зберегти
              </button>
              <button
                onClick={() => setEditingId(null)}
                className="rounded-lg border border-ink/15 px-3 py-1.5 text-xs font-medium text-ink/60 transition hover:bg-ink/5"
              >
                Скасувати
              </button>
            </div>
          </div>
        ) : (
          <AmountRow
            key={p.id}
            title={<span className="text-sm font-normal">{names[p.debt_id] ?? "Борг"}</span>}
            meta={formatDateUk(p.paid_on)}
            amount={formatUah(p.amount)}
            amountClassName="text-sm text-ink"
            className="rounded-lg border border-ink/10 px-3 py-2"
            actions={
              editable ? (
                <>
                  <RowAction onClick={() => startEdit(p)} disabled={isBusy}>
                    Змінити
                  </RowAction>
                  <RowAction onClick={() => handleDelete(p)} disabled={isBusy} danger>
                    Видалити
                  </RowAction>
                </>
              ) : null
            }
          />
        )
      )}
      {!editable && payments.length > 0 && (
        <p className="text-xs text-ink/50">Цей місяць закрито для змін: він належить попередній версії плану.</p>
      )}
      {actionError && <p className="rounded-lg bg-brick/10 px-3 py-2 text-sm text-brick">{actionError}</p>}
    </div>
  );
}

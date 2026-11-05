import { useState, useEffect, useRef } from "react";
import api from "../services/api";
import PlanFact from "../components/PlanFact";
import AmountRow, { RowAction } from "../components/ui/AmountRow";
import { formatMonth, parseExtraInput, responseDetail } from "../utils/affordability";
import { formatUah } from "../utils/money";
import {
  MATCHES_AVALANCHE_NOTE,
  MATERIALIZE_BUTTON_LABEL,
  NO_SCHEDULE_TOTALS_NOTE,
  STRATEGY_LABELS,
  acceptErrorText,
  acceptedPlanView,
  compareStrategies,
  currentMonthPayments,
  draftDetailsCaption,
  draftView,
  isPaidOff,
  materializeAction,
  materializeErrorText,
  materializeTooEarlyText,
  milpMatchesAvalanche,
  notPaidOffLabel,
  todayKyiv,
  versionTotals,
} from "../utils/debtPlan";

// Калькулятор спроможності: додатковий платіж задає користувач; панель
// показує лише історію, без прогнозу і без рекомендованої суми.
const AFFORDABILITY_DEBOUNCE_MS = 500;
const AFFORDABILITY_MONTHS = 12;

// Категорії боргу для користувача; кожна відповідає одному з двох типів, які
// розрізняють оптимізатори (debt_type: revolving | installment). МФО
// моделюється як revolving (щомісячний відсоток).
const DEBT_CATEGORIES = [
  { id: "credit_card", label: "Кредитна картка", debt_type: "revolving", defaultRate: "0.04" },
  { id: "mfo", label: "МФО / швидка позика", debt_type: "revolving", defaultRate: "0.10" },
  { id: "installment_plan", label: "Розстрочка (0%)", debt_type: "installment", defaultRate: "0" },
  { id: "car_loan", label: "Автокредит", debt_type: "installment", defaultRate: "0.02" },
  { id: "mortgage", label: "Іпотека", debt_type: "installment", defaultRate: "0.01" },
  { id: "personal_loan", label: "Позика у знайомих/родини", debt_type: "installment", defaultRate: "0" },
  { id: "other", label: "Інше (обрати вручну)", debt_type: null, defaultRate: "" },
];

const emptyForm = {
  name: "",
  balance: "",
  monthly_rate: "",
  min_payment: "",
  category: "credit_card",
  debt_type: "revolving", // тільки для "Інше"
};

// Ті самі правила, що в app/api/debt.py (DebtCreateRequest, DebtUpdateRequest);
// значення продубльовані, остаточну перевірку робить сервер.
const DEBT_NAME_MAX_LEN = 100;
const DEBT_BALANCE_MAX_UAH = 1_000_000_000;
const DEBT_MONTHLY_RATE_MAX = 1;
const DEBT_FORM_FIELDS = ["name", "debt_type", "balance", "monthly_rate", "min_payment"];

function parseNumberInput(raw) {
  const text = String(raw ?? "").trim();
  if (text === "") return { value: null, error: "Обов'язкове поле." };
  const value = Number(text);
  if (!Number.isFinite(value)) return { value: null, error: "Введіть число." };
  return { value, error: null };
}

// Клієнтська валідація боргу для форм додавання і редагування.
// Повертає { payload, errors }: errors — повідомлення за назвою поля.
function validateDebtForm(form) {
  const errors = {};
  const name = form.name.trim();
  if (name.length < 1 || name.length > DEBT_NAME_MAX_LEN) {
    errors.name = `Назва — від 1 до ${DEBT_NAME_MAX_LEN} символів.`;
  }

  const balance = parseNumberInput(form.balance);
  if (balance.error) errors.balance = balance.error;
  else if (balance.value <= 0 || balance.value > DEBT_BALANCE_MAX_UAH) {
    errors.balance = "Залишок має бути більшим за 0 і не більшим за 1 000 000 000 грн. Погашений борг позначте кнопкою «Погашено».";
  }

  const rate = parseNumberInput(form.monthly_rate);
  if (rate.error) errors.monthly_rate = rate.error;
  else if (rate.value < 0 || rate.value > DEBT_MONTHLY_RATE_MAX) {
    errors.monthly_rate = "Ставка — частка за місяць від 0 до 1 (0.035 = 3.5%).";
  }

  let minPayment = null;
  if (form.debt_type === "installment") {
    const parsed = parseNumberInput(form.min_payment);
    if (parsed.error) errors.min_payment = parsed.error;
    else if (parsed.value <= 0) errors.min_payment = "Для розстрочки/кредиту мінімальний платіж має бути більшим за 0.";
    else minPayment = parsed.value;
  }

  return {
    errors,
    payload: {
      name,
      debt_type: form.debt_type,
      balance: balance.value,
      monthly_rate: rate.value,
      min_payment: minPayment,
    },
  };
}

// 422 від FastAPI — масив {loc, msg}; помилки полів форми йдуть до полів,
// решта (і рядковий detail, напр. 404) — загальним повідомленням.
function splitServerErrors(err, fallback) {
  const detail = err.response?.data?.detail;
  const fields = {};
  const general = [];
  if (Array.isArray(detail)) {
    for (const d of detail) {
      const field = Array.isArray(d?.loc) ? d.loc[d.loc.length - 1] : null;
      if (DEBT_FORM_FIELDS.includes(field)) fields[field] = d.msg;
      else if (d?.msg) general.push(d.msg);
    }
  } else if (typeof detail === "string") {
    general.push(detail);
  }
  if (!Object.keys(fields).length && !general.length) {
    general.push(fallback + (err.response?.status ? ` (HTTP ${err.response.status}).` : "."));
  }
  return { fields, general: general.join("; ") || null };
}

export default function Debts() {
  const [debts, setDebts] = useState([]);
  const [plan, setPlan] = useState(null);
  const [planError, setPlanError] = useState(false);
  const [isLoading, setIsLoading] = useState(true);

  const [form, setForm] = useState(emptyForm);
  const [formError, setFormError] = useState(null);
  const [formErrors, setFormErrors] = useState({});
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [showFullSchedule, setShowFullSchedule] = useState(false);

  const [editingId, setEditingId] = useState(null);
  const [editForm, setEditForm] = useState(null);
  const [editErrors, setEditErrors] = useState({});
  const [editError, setEditError] = useState(null);
  const [isSavingEdit, setIsSavingEdit] = useState(false);

  const [extraInput, setExtraInput] = useState("0");
  const extra = parseExtraInput(extraInput);
  const [affordability, setAffordability] = useState(null);
  const [affordabilityError, setAffordabilityError] = useState(null);
  const [isAffordabilityLoading, setIsAffordabilityLoading] = useState(false);

  const [isBuildingPlan, setIsBuildingPlan] = useState(false);
  const buildInFlight = useRef(false);
  const [planBuildError, setPlanBuildError] = useState(null);

  // Прийнята версія плану (GET /debt/plan/active); null — прийнятого плану немає.
  const [active, setActive] = useState(null);
  const [isAccepting, setIsAccepting] = useState(false);
  const acceptInFlight = useRef(false);
  const [acceptError, setAcceptError] = useState(null);
  const [isMaterializing, setIsMaterializing] = useState(false);
  const materializeInFlight = useRef(false);
  const [materializeError, setMaterializeError] = useState(null);
  // Автоматична фіксація — один раз за відкриття сторінки.
  const autoMaterializeTried = useRef(false);

  const today = todayKyiv();

  async function loadAll() {
    setIsLoading(true);
    const [debtsRes, planRes, activeRes] = await Promise.allSettled([
      api.get("/debt"),
      api.get("/debt/plan/latest"),
      api.get("/debt/plan/active"),
    ]);

    if (debtsRes.status === "fulfilled") setDebts(debtsRes.value.data);

    if (planRes.status === "fulfilled") {
      // Якщо план не містить extra_monthly_uah, значення не підставляємо.
      setPlan({ ...planRes.value.data, source: "saved" });
      setPlanError(false);
    } else {
      setPlanError(true);
    }

    const activeVersion = activeRes.status === "fulfilled" ? activeRes.value.data : null;
    setActive(activeVersion);
    setIsLoading(false);

    if (!autoMaterializeTried.current && materializeAction(activeVersion, todayKyiv()) === "auto") {
      autoMaterializeTried.current = true;
      await handleMaterialize(false);
    }
  }

  useEffect(() => {
    loadAll();
  }, []);

  // Прийняти поточну чернетку; прийнятий план до цього лишається чинним.
  async function handleAccept() {
    if (acceptInFlight.current || !plan?.plan_id) return;
    acceptInFlight.current = true;
    setIsAccepting(true);
    setAcceptError(null);
    try {
      await api.post(`/debt/plans/${plan.plan_id}/accept`);
      setMaterializeError(null);
      await reloadActive();
    } catch (err) {
      setAcceptError(acceptErrorText(err.response?.status, responseDetail(err)));
    } finally {
      acceptInFlight.current = false;
      setIsAccepting(false);
    }
  }

  // Відповіді accept і materialize — сама версія, без effective/pending; тож після них
  // прийнятий план перечитується з GET /debt/plan/active.
  async function reloadActive() {
    try {
      const res = await api.get("/debt/plan/active");
      setActive(res.data);
    } catch {
      setActive(null);
    }
  }

  // Після платежу, звірки чи позначки місяця: залишки боргів і актуальність чернетки
  // змінюються, тож список боргів і чернетка перечитуються без перезавантаження сторінки.
  async function refreshAfterLedgerChange() {
    const [debtsRes, planRes] = await Promise.allSettled([api.get("/debt"), api.get("/debt/plan/latest")]);
    if (debtsRes.status === "fulfilled") setDebts(debtsRes.value.data);
    if (planRes.status === "fulfilled") setPlan({ ...planRes.value.data, source: "saved" });
  }

  // Зафіксувати розклад прийнятої версії (POST /debt/plan/materialize).
  async function handleMaterialize(force) {
    if (materializeInFlight.current) return;
    materializeInFlight.current = true;
    setIsMaterializing(true);
    setMaterializeError(null);
    try {
      await api.post("/debt/plan/materialize", null, { params: force ? { force: true } : {} });
      await reloadActive();
    } catch (err) {
      setMaterializeError(materializeErrorText(err.response?.status, responseDetail(err)));
    } finally {
      materializeInFlight.current = false;
      setIsMaterializing(false);
    }
  }

  // Історична спроможність: лише при валідній сумі, з debounce; попередній
  // запит скасовується, тож у мережу йде тільки останнє значення.
  useEffect(() => {
    if (extra.value === null) {
      setAffordability(null);
      setAffordabilityError(null);
      return undefined;
    }
    const controller = new AbortController();
    const timer = setTimeout(async () => {
      setIsAffordabilityLoading(true);
      setAffordabilityError(null);
      try {
        const res = await api.get("/debt/affordability", {
          params: { extra: extra.value, months: AFFORDABILITY_MONTHS },
          signal: controller.signal,
        });
        setAffordability(res.data);
      } catch (err) {
        if (controller.signal.aborted) return;
        setAffordability(null);
        setAffordabilityError(
          responseDetail(err) || "Не вдалося отримати історію: сервер недоступний або сталася помилка."
        );
      } finally {
        if (!controller.signal.aborted) setIsAffordabilityLoading(false);
      }
    }, AFFORDABILITY_DEBOUNCE_MS);
    return () => {
      clearTimeout(timer);
      controller.abort();
    };
  }, [extra.value]);

  async function handleAddDebt(e) {
    e.preventDefault();
    const selectedCategory = DEBT_CATEGORIES.find((c) => c.id === form.category);
    const resolvedDebtType = selectedCategory.debt_type || form.debt_type;
    const { errors, payload } = validateDebtForm({ ...form, debt_type: resolvedDebtType });
    setFormErrors(errors);
    setFormError(null);
    if (Object.keys(errors).length) return;
    setIsSubmitting(true);
    try {
      await api.post("/debt", payload);
      setForm(emptyForm);
      await loadAll();
    } catch (err) {
      const { fields, general } = splitServerErrors(err, "Не вдалося додати борг");
      setFormErrors(fields);
      setFormError(general);
    } finally {
      setIsSubmitting(false);
    }
  }

  async function handleDeactivate(id) {
    if (!confirm("Позначити борг як погашений? Історія лишиться, але він зникне зі списку активних.")) {
      return;
    }
    try {
      await api.patch(`/debt/${id}`, { is_active: false });
      await loadAll();
    } catch (err) {
      alert(err.response?.data?.detail || "Не вдалося оновити борг.");
    }
  }

  function startEdit(debt) {
    setEditErrors({});
    setEditError(null);
    setEditForm({
      name: debt.name,
      debt_type: debt.debt_type,
      balance: String(debt.balance),
      monthly_rate: String(debt.monthly_rate),
      min_payment: debt.min_payment ?? "",
    });
    setEditingId(debt.id);
  }

  function cancelEdit() {
    setEditingId(null);
    setEditForm(null);
    setEditErrors({});
    setEditError(null);
  }

  async function handleSaveEdit(e, id) {
    e.preventDefault();
    const { errors, payload } = validateDebtForm(editForm);
    setEditErrors(errors);
    setEditError(null);
    if (Object.keys(errors).length) return;
    setIsSavingEdit(true);
    try {
      await api.patch(`/debt/${id}`, payload);
      cancelEdit();
      // Перечитує і борги, і /debt/plan/latest — попередження про застарілий
      // план з'являється одразу, якщо план уже було збудовано.
      await loadAll();
    } catch (err) {
      const { fields, general } = splitServerErrors(err, "Не вдалося зберегти зміни");
      setEditErrors(fields);
      setEditError(general);
    } finally {
      setIsSavingEdit(false);
    }
  }

  // Лише за кліком; ref не пускає другий POST, навіть якщо кліки встигли до
  // перерендеру з disabled-кнопкою.
  async function handleBuildPlan() {
    if (buildInFlight.current || extra.value === null) return;
    buildInFlight.current = true;
    setIsBuildingPlan(true);
    setPlanBuildError(null);
    try {
      const res = await api.post("/debt/optimize", { extra_monthly_uah: extra.value });
      setPlan({ ...res.data, source: "fresh" });
      setPlanError(false);
      setAcceptError(null);
      setShowFullSchedule(false);
    } catch (err) {
      const status = err.response?.status;
      const detail = responseDetail(err);
      if (status === 409) {
        setPlanBuildError({ kind: "no_debts", text: detail || "Немає активних боргів." });
      } else if (status === 422 || status === 503) {
        setPlanBuildError({ kind: "detail", text: detail || "План не вдалося побудувати." });
      } else {
        setPlanBuildError({
          kind: "general",
          text:
            "Не вдалося побудувати план: сервер недоступний або сталася помилка" +
            (status ? ` (HTTP ${status})` : "") +
            (detail ? `: ${detail}` : "."),
        });
      }
    } finally {
      buildInFlight.current = false;
      setIsBuildingPlan(false);
    }
  }

  if (isLoading) {
    return <p className="text-ink/50">Завантаження...</p>;
  }

  const selectedCategoryObj = DEBT_CATEGORIES.find((c) => c.id === form.category);
  const resolvedDebtType = selectedCategoryObj.debt_type || form.debt_type;
  const acceptedView = acceptedPlanView(active, today);
  const acceptedPayments = acceptedView ? currentMonthPayments(acceptedView.shown, today) : null;
  const draftBlock = draftView(planError ? null : plan, active);
  const materializeState = materializeAction(active, today);
  // Числа чернетки ховаються, якщо змінився склад боргів; «Скільки платити цього місяця»
  // з чернетки — лише коли прийнятий план не показує свого розкладу за поточний місяць.
  const showDraftNumbers = !draftBlock || draftBlock.showNumbers;
  const showDraftThisMonth = showDraftNumbers && (draftBlock || !acceptedPayments);

  return (
    <div className="space-y-6">
      <h1 className="font-display text-2xl font-semibold text-forest">Борги</h1>

      {/* Список активних боргів */}
      <section className="rounded-xl border border-ink/10 bg-white p-5">
        <h2 className="mb-4 text-sm font-medium uppercase tracking-wide text-ink/50">
          Активні борги
        </h2>
        {debts.length === 0 ? (
          <p className="text-sm text-ink/60">Ще немає жодного боргу.</p>
        ) : (
          <div className="space-y-3">
            {debts.map((d) =>
              editingId === d.id ? (
                <form
                  key={d.id}
                  onSubmit={(e) => handleSaveEdit(e, d.id)}
                  noValidate
                  className="space-y-3 rounded-lg border border-forest/30 bg-forest/5 p-3"
                >
                  <DebtEditFields form={editForm} setForm={setEditForm} errors={editErrors} />

                  {editError && (
                    <p className="rounded-lg bg-brick/10 px-3 py-2 text-sm text-brick">{editError}</p>
                  )}

                  <div className="flex gap-2">
                    <button
                      type="submit"
                      disabled={isSavingEdit}
                      className="flex-1 rounded-lg bg-forest px-4 py-2.5 font-medium text-paper transition hover:bg-forest-light disabled:opacity-50"
                    >
                      {isSavingEdit ? "Зберігаємо..." : "Зберегти"}
                    </button>
                    <button
                      type="button"
                      onClick={cancelEdit}
                      className="rounded-lg border border-ink/15 px-4 py-2.5 font-medium text-ink/60 transition hover:bg-ink/5"
                    >
                      Скасувати
                    </button>
                  </div>
                </form>
              ) : (
                <AmountRow
                  key={d.id}
                  title={d.name}
                  meta={
                    <>
                      {d.debt_type === "revolving" ? "Кредитна лінія" : "Розстрочка/кредит"} •{" "}
                      {(d.monthly_rate * 100).toFixed(1)}% на місяць
                      {d.min_payment ? (
                        <>
                          {" "}• мін. платіж <span className="whitespace-nowrap">{formatUah(d.min_payment)}</span>
                        </>
                      ) : null}
                    </>
                  }
                  amount={formatUah(d.balance)}
                  amountClassName="text-lg text-brick"
                  actions={
                    <>
                      <RowAction onClick={() => startEdit(d)}>Редагувати</RowAction>
                      <RowAction onClick={() => handleDeactivate(d.id)} danger>
                        Погашено
                      </RowAction>
                    </>
                  }
                />
              )
            )}
          </div>
        )}
      </section>

      {/* Додатковий платіж і історична спроможність */}
      <section className="rounded-xl border border-ink/10 bg-white p-5">
        <h2 className="mb-4 text-sm font-medium uppercase tracking-wide text-ink/50">
          Додатковий платіж
        </h2>
        <label className="block text-sm font-medium text-ink" htmlFor="extra-payment">
          Додатковий платіж на місяць, грн
        </label>
        <p className="mb-1 text-xs text-ink/50">
          Понад обов'язкові платежі за всіма боргами. 0 — лише обов'язкові платежі.
        </p>
        <div className="flex flex-wrap items-start gap-2">
          <input
            id="extra-payment"
            type="text"
            inputMode="decimal"
            value={extraInput}
            onChange={(e) => setExtraInput(e.target.value)}
            aria-invalid={extra.error ? "true" : "false"}
            className="w-48 rounded-lg border border-ink/15 bg-white px-3 py-2 font-mono outline-none focus:border-forest focus:ring-1 focus:ring-forest"
          />
          <button
            onClick={handleBuildPlan}
            disabled={isBuildingPlan || extra.value === null}
            className="rounded-lg bg-forest px-4 py-2 text-sm font-medium text-paper transition hover:bg-forest-light disabled:opacity-50"
          >
            {isBuildingPlan ? "Будуємо план..." : active ? "Побудувати новий план" : "Побудувати план"}
          </button>
        </div>
        {extra.error && <p className="mt-1 text-xs text-brick">{extra.error}</p>}

        {planBuildError && (
          <div className="mt-3 rounded-lg bg-brick/10 px-3 py-2 text-sm text-brick">
            <p>{planBuildError.text}</p>
            {planBuildError.kind === "no_debts" && (
              <a href="#add-debt" className="mt-1 inline-block font-medium underline">
                Додати борг
              </a>
            )}
          </div>
        )}

        <div className="mt-5 rounded-lg border border-ink/10 p-4">
          <h3 className="mb-2 text-sm font-medium text-ink">Історична спроможність</h3>
          {extra.value === null ? (
            <p className="text-xs text-ink/50">Вкажи коректну суму, щоб побачити історію.</p>
          ) : isAffordabilityLoading && !affordability ? (
            <p className="text-xs text-ink/50">Завантаження...</p>
          ) : affordabilityError ? (
            <p className="text-sm text-brick">{affordabilityError}</p>
          ) : affordability ? (
            <AffordabilityPanel data={affordability} extraValue={extra.value} />
          ) : null}
        </div>
      </section>

      {/* Форма додавання боргу */}
      <section id="add-debt" className="rounded-xl border border-ink/10 bg-white p-5">
        <h2 className="mb-4 text-sm font-medium uppercase tracking-wide text-ink/50">
          Додати борг
        </h2>
        <form onSubmit={handleAddDebt} noValidate className="space-y-3">
          <div>
            <label className="mb-1 block text-sm font-medium text-ink">Назва</label>
            <input
              type="text"
              required
              value={form.name}
              onChange={(e) => setForm({ ...form, name: e.target.value })}
              className="w-full rounded-lg border border-ink/15 bg-white px-3 py-2 outline-none focus:border-forest focus:ring-1 focus:ring-forest"
              placeholder="напр. Кредитна картка Monobank"
            />
            <FieldError text={formErrors.name} />
          </div>

          <div>
            <label className="mb-1 block text-sm font-medium text-ink">Тип боргу</label>
            <select
              value={form.category}
              onChange={(e) => {
                const cat = DEBT_CATEGORIES.find((c) => c.id === e.target.value);
                setForm({
                  ...form,
                  category: e.target.value,
                  monthly_rate: cat.defaultRate || form.monthly_rate,
                });
              }}
              className="w-full rounded-lg border border-ink/15 bg-white px-3 py-2 outline-none focus:border-forest focus:ring-1 focus:ring-forest"
            >
              {DEBT_CATEGORIES.map((c) => (
                <option key={c.id} value={c.id}>
                  {c.label}
                </option>
              ))}
            </select>
            <FieldError text={formErrors.debt_type} />
          </div>

          {selectedCategoryObj.id === "other" && (
            <div>
              <label className="mb-1 block text-sm font-medium text-ink">
                Механіка мінімального платежу
              </label>
              <select
                value={form.debt_type}
                onChange={(e) => setForm({ ...form, debt_type: e.target.value })}
                className="w-full rounded-lg border border-ink/15 bg-white px-3 py-2 outline-none focus:border-forest focus:ring-1 focus:ring-forest"
              >
                <option value="revolving">Кредитна лінія (мін. платіж рахується автоматично)</option>
                <option value="installment">Розстрочка/кредит (фіксований мін. платіж)</option>
              </select>
            </div>
          )}

          <p className="text-xs text-ink/40">
            {resolvedDebtType === "revolving"
              ? "Мінімальний платіж рахується автоматично (4% від балансу, мін. 200 грн)"
              : "Потрібен фіксований мінімальний платіж (вкажи нижче)"}
          </p>

          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="mb-1 block text-sm font-medium text-ink">Баланс, грн</label>
              <input
                type="number"
                step="0.01"
                required
                value={form.balance}
                onChange={(e) => setForm({ ...form, balance: e.target.value })}
                className="w-full rounded-lg border border-ink/15 bg-white px-3 py-2 font-mono outline-none focus:border-forest focus:ring-1 focus:ring-forest"
              />
              <FieldError text={formErrors.balance} />
            </div>
            <div>
              <label className="mb-1 block text-sm font-medium text-ink">Ставка на місяць</label>
              <input
                type="number"
                step="0.001"
                required
                value={form.monthly_rate}
                onChange={(e) => setForm({ ...form, monthly_rate: e.target.value })}
                className="w-full rounded-lg border border-ink/15 bg-white px-3 py-2 font-mono outline-none focus:border-forest focus:ring-1 focus:ring-forest"
                placeholder="0.035 = 3.5%"
              />
              <FieldError text={formErrors.monthly_rate} />
            </div>
          </div>

          {resolvedDebtType === "installment" && (
            <div>
              <label className="mb-1 block text-sm font-medium text-ink">
                Мінімальний платіж, грн
              </label>
              <input
                type="number"
                step="0.01"
                required
                value={form.min_payment}
                onChange={(e) => setForm({ ...form, min_payment: e.target.value })}
                className="w-full rounded-lg border border-ink/15 bg-white px-3 py-2 font-mono outline-none focus:border-forest focus:ring-1 focus:ring-forest"
              />
              <FieldError text={formErrors.min_payment} />
            </div>
          )}

          {formError && (
            <p className="rounded-lg bg-brick/10 px-3 py-2 text-sm text-brick">{formError}</p>
          )}

          <button
            type="submit"
            disabled={isSubmitting}
            className="w-full rounded-lg bg-forest px-4 py-2.5 font-medium text-paper transition hover:bg-forest-light disabled:opacity-50"
          >
            {isSubmitting ? "Додаємо..." : "Додати борг"}
          </button>
        </form>
      </section>

      {/* Повний план погашення */}
      <section className="rounded-xl border border-ink/10 bg-white p-5">
        <h2 className="mb-4 text-sm font-medium uppercase tracking-wide text-ink/50">
          План погашення
        </h2>

        {!active && planError ? (
          <p className="text-sm text-ink/60">
            Плану ще немає. Вкажи додатковий платіж і натисни «Побудувати план».
          </p>
        ) : (
          <div className="space-y-4">
            {acceptedView && (
              <div className="space-y-2 border-b border-ink/10 pb-4 text-sm">
                <p className="text-xs font-medium uppercase tracking-wide text-forest">Прийнятий план</p>
                {acceptedView.messages
                  .filter((m) => m.key !== "structure_changed")
                  .map((m) => (
                    <p key={m.key} className="text-ink/80">{m.text}</p>
                  ))}
                <MonthlyTotals totals={versionTotals(acceptedView.shown)} />
                {!acceptedView.shown.materialized && (
                  <p className="text-xs text-ink/50">{NO_SCHEDULE_TOTALS_NOTE}</p>
                )}
                {acceptedView.shown.materialized && acceptedView.shown.months_to_payoff ? (
                  <p className="text-xs text-ink/60">
                    Місяців до погашення: <span className="font-mono">{acceptedView.shown.months_to_payoff}</span> •
                    Переплата за планом:{" "}
                    <span className="whitespace-nowrap font-mono">{formatUah(acceptedView.shown.total_interest)}</span>
                  </p>
                ) : null}
                {acceptedPayments?.length > 0 && (
                  <div className="space-y-2 pt-1">
                    <p className="text-xs text-ink/50">Скільки платити цього місяця</p>
                    {acceptedPayments.map((d) => (
                      <PaymentRow key={d.debt_id} name={d.name} payment={d.payment} remaining={d.closing} />
                    ))}
                  </div>
                )}
                {acceptedView.messages
                  .filter((m) => m.key === "structure_changed")
                  .map((m) => (
                    <p key={m.key} className="rounded-lg bg-gold/15 px-3 py-2 text-ink/80">{m.text}</p>
                  ))}
                {isMaterializing && <p className="text-xs text-ink/50">Фіксуємо план...</p>}
                {!isMaterializing && materializeState === "too_early" && (
                  <div className="space-y-2 rounded-lg bg-ink/5 px-3 py-2">
                    <p className="text-ink/80">{materializeTooEarlyText(active)}</p>
                    <button
                      onClick={() => handleMaterialize(true)}
                      className="rounded-lg border border-forest/40 px-3 py-1.5 text-xs font-medium text-forest transition hover:bg-forest/5"
                    >
                      {MATERIALIZE_BUTTON_LABEL}
                    </button>
                  </div>
                )}
                {materializeError && (
                  <p className="rounded-lg bg-ink/5 px-3 py-2 text-ink/80">{materializeError}</p>
                )}
              </div>
            )}

            {draftBlock && (
              <div className="space-y-2 border-b border-ink/10 pb-4 text-sm">
                <p className="text-xs font-medium uppercase tracking-wide text-ink/50">Чернетка</p>
                {draftBlock.notes.map((note, i) => (
                  <p
                    key={note}
                    className={
                      draftBlock.status !== "ok" && i === 0
                        ? "rounded-lg bg-gold/15 px-3 py-2 text-ink/80"
                        : i === 0
                          ? "text-ink/80"
                          : "text-xs text-ink/50"
                    }
                  >
                    {note}
                  </p>
                ))}
                <MonthlyTotals totals={draftBlock.totals} />
                {draftBlock.canAccept && (
                  <button
                    onClick={handleAccept}
                    disabled={isAccepting}
                    className="rounded-lg bg-forest px-4 py-2 text-sm font-medium text-paper transition hover:bg-forest-light disabled:opacity-50"
                  >
                    {isAccepting ? "Приймаємо..." : "Прийняти план"}
                  </button>
                )}
                {acceptError && (
                  <p className="rounded-lg bg-brick/10 px-3 py-2 text-brick">{acceptError}</p>
                )}
              </div>
            )}

            {!planError && (
              <>
                <p className="text-xs font-medium uppercase tracking-wide text-ink/50">
                  {draftDetailsCaption(plan, active)}
                </p>
                {plan.plan_note && (
                  <p className="rounded-lg bg-gold/15 px-3 py-2 text-sm text-ink/80">{plan.plan_note}</p>
                )}
                <div className="flex items-baseline justify-between border-b border-ink/10 pb-4">
                  <div>
                    <p className="text-xs text-ink/50">
                      Місяців до погашення
                      {draftBlock?.numbersCaption ? ` (${draftBlock.numbersCaption})` : ""}
                    </p>
                    {!showDraftNumbers ? (
                      <p className="font-mono text-2xl text-ink/40">—</p>
                    ) : plan.months_to_payoff ? (
                      <p className="font-mono text-2xl font-semibold text-forest">{plan.months_to_payoff}</p>
                    ) : (
                      <p className="text-sm font-medium text-brick">{notPaidOffLabel(plan.horizon_months)}</p>
                    )}
                  </div>
                  {!showDraftNumbers ? (
                    <div className="text-right">
                      <p className="text-xs text-ink/50">Переплата за планом</p>
                      <p className="font-mono text-lg text-ink/40">—</p>
                    </div>
                  ) : plan.months_to_payoff ? (
                    <div className="text-right">
                      <p className="text-xs text-ink/50">
                        Переплата за планом
                        {draftBlock?.numbersCaption ? ` (${draftBlock.numbersCaption})` : ""}
                      </p>
                      <p className="font-mono text-lg text-ink">{formatUah(plan.total_interest_paid)}</p>
                    </div>
                  ) : null}
                </div>

                {showDraftThisMonth && plan.monthly_schedule?.length > 0 && (
                  <div className="space-y-2 border-b border-ink/10 pb-4">
                    <p className="text-xs text-ink/50">Скільки платити цього місяця</p>
                    {Object.entries(plan.monthly_schedule[0].payments)
                      .filter(([, amount]) => amount > 0)
                      .sort((a, b) => b[1] - a[1])
                      .map(([debtName, paymentAmount]) => (
                        <PaymentRow
                          key={debtName}
                          name={debtName}
                          payment={paymentAmount}
                          remaining={plan.monthly_schedule[0].balances[debtName]}
                        />
                      ))}
                  </div>
                )}

                {showDraftNumbers && plan.monthly_schedule?.length > 1 && (
                  <div className="space-y-2 border-b border-ink/10 pb-4">
                    <button
                      onClick={() => setShowFullSchedule((v) => !v)}
                      className="text-xs font-medium text-forest underline decoration-forest/30 underline-offset-2 hover:decoration-forest"
                    >
                      {showFullSchedule
                        ? "Сховати повний графік"
                        : `Показати повний графік (${plan.monthly_schedule.length} міс.)`}
                    </button>

                    {showFullSchedule && (
                      <div className="space-y-2 pt-1">
                        {plan.monthly_schedule.slice(1).map((monthEntry) => (
                          <details
                            key={monthEntry.month}
                            className="rounded-lg border border-ink/10 p-3"
                          >
                            <summary className="cursor-pointer text-sm text-ink">
                              Місяць {monthEntry.month} — залишок{" "}
                              <span className="whitespace-nowrap">{formatUah(monthEntry.total_balance)}</span>
                            </summary>
                            <div className="mt-2 space-y-1">
                              {Object.entries(monthEntry.payments)
                                .filter(([, amount]) => amount > 0)
                                .sort((a, b) => b[1] - a[1])
                                .map(([debtName, paymentAmount]) => (
                                  <div
                                    key={debtName}
                                    className="flex items-center justify-between gap-3 text-xs"
                                  >
                                    <span className="min-w-0 text-ink/70 [overflow-wrap:anywhere]">{debtName}</span>
                                    <div className="shrink-0 text-right">
                                      <span className="whitespace-nowrap font-mono text-ink">
                                        {formatUah(paymentAmount)}
                                      </span>
                                      <span className="block whitespace-nowrap text-ink/40 sm:ml-2 sm:inline">
                                        залишиться {formatUah(monthEntry.balances[debtName])}
                                      </span>
                                    </div>
                                  </div>
                                ))}
                            </div>
                          </details>
                        ))}
                      </div>
                    )}
                  </div>
                )}

                {showDraftNumbers && (
                <div className="space-y-2">
                  <p className="text-xs text-ink/50">
                    Порівняння всіх стратегій
                    {draftBlock?.numbersCaption ? ` (${draftBlock.numbersCaption})` : ""}
                  </p>
                  {Object.entries(plan.comparison || {})
                    .sort(compareStrategies)
                    .map(([strategy, data]) => (
                      <div
                        key={strategy}
                        className={`flex items-center justify-between gap-3 rounded-lg px-3 py-2 ${
                          strategy === "milp" ? "bg-gold/10" : "bg-ink/5"
                        }`}
                      >
                        <div className="min-w-0">
                          <span className={strategy === "milp" ? "font-medium text-forest" : "text-ink/70"}>
                            {STRATEGY_LABELS[strategy] || strategy}
                          </span>
                          {strategy === "milp" && milpMatchesAvalanche(plan.comparison) && (
                            <p className="text-xs text-ink/50">{MATCHES_AVALANCHE_NOTE}</p>
                          )}
                        </div>
                        {isPaidOff(data) ? (
                          <div className="shrink-0 text-right">
                            <p className="text-xs text-ink/50">Переплата</p>
                            <p className="whitespace-nowrap">
                              <span className="font-mono text-sm">{formatUah(data.total_interest_paid)}</span>
                              <span className="ml-2 text-xs text-ink/40">{data.months_to_payoff} міс.</span>
                            </p>
                          </div>
                        ) : (
                          <span className="shrink-0 text-right text-xs text-brick">
                            {notPaidOffLabel(plan.horizon_months)}
                          </span>
                        )}
                      </div>
                    ))}
                </div>
                )}
              </>
            )}
          </div>
        )}
      </section>

      {/* key: після прийняття чи фіксації версії блок завантажується заново. */}
      {active && (
        <PlanFact
          key={`${active.version_id}:${active.effective?.version_id}:${active.materialized}`}
          debts={debts}
          today={today}
          onLedgerChange={refreshAfterLedgerChange}
        />
      )}
    </div>
  );
}

// Рядок «Скільки платити цього місяця»: борг, плановий платіж і залишок після нього.
function PaymentRow({ name, payment, remaining }) {
  return (
    <AmountRow
      title={<span className="text-sm font-normal">{name}</span>}
      meta={<span className="whitespace-nowrap">залишиться {formatUah(remaining)}</span>}
      amount={formatUah(payment)}
      amountClassName="text-sm font-semibold text-forest"
      className="rounded-lg bg-forest/5 px-3 py-2"
    />
  );
}

// «Обов'язкові платежі / Додатковий платіж / Разом на місяць» — однаково для чернетки й прийнятого плану.
function MonthlyTotals({ totals }) {
  return (
    <div className="grid grid-cols-3 gap-3">
      <div>
        <p className="text-xs text-ink/50">Обов'язкові платежі</p>
        <p className="font-mono">{formatUah(totals.min)}</p>
      </div>
      <div>
        <p className="text-xs text-ink/50">Додатковий платіж</p>
        <p className="font-mono">{formatUah(totals.extra)}</p>
      </div>
      <div>
        <p className="text-xs text-ink/50">Разом на місяць</p>
        <p className="font-mono font-semibold text-forest">{formatUah(totals.total)}</p>
      </div>
    </div>
  );
}

const INPUT_CLASS =
  "w-full rounded-lg border bg-white px-3 py-2 outline-none focus:border-forest focus:ring-1 focus:ring-forest";

function FieldError({ text }) {
  return text ? <p className="mt-1 text-xs text-brick">{text}</p> : null;
}

function DebtEditFields({ form, setForm, errors }) {
  const border = (field) => (errors[field] ? "border-brick" : "border-ink/15");
  return (
    <>
      <div>
        <label className="mb-1 block text-sm font-medium text-ink">Назва</label>
        <input
          type="text"
          value={form.name}
          onChange={(e) => setForm({ ...form, name: e.target.value })}
          className={`${INPUT_CLASS} ${border("name")}`}
        />
        <FieldError text={errors.name} />
      </div>

      <div>
        <label className="mb-1 block text-sm font-medium text-ink">Механіка мінімального платежу</label>
        <select
          value={form.debt_type}
          onChange={(e) => setForm({ ...form, debt_type: e.target.value })}
          className={`${INPUT_CLASS} ${border("debt_type")}`}
        >
          <option value="revolving">Кредитна лінія (мін. платіж рахується автоматично)</option>
          <option value="installment">Розстрочка/кредит (фіксований мін. платіж)</option>
        </select>
        <FieldError text={errors.debt_type} />
      </div>

      <div className="grid grid-cols-2 gap-3">
        <div>
          <label className="mb-1 block text-sm font-medium text-ink">Залишок, грн</label>
          <input
            type="number"
            step="0.01"
            value={form.balance}
            onChange={(e) => setForm({ ...form, balance: e.target.value })}
            className={`${INPUT_CLASS} font-mono ${border("balance")}`}
          />
          <p className="mt-1 text-xs text-ink/40">
            Вкажіть фактичний залишок за випискою банку (з урахуванням нарахованих відсотків)
          </p>
          <FieldError text={errors.balance} />
        </div>
        <div>
          <label className="mb-1 block text-sm font-medium text-ink">Ставка на місяць</label>
          <input
            type="number"
            step="0.001"
            value={form.monthly_rate}
            onChange={(e) => setForm({ ...form, monthly_rate: e.target.value })}
            className={`${INPUT_CLASS} font-mono ${border("monthly_rate")}`}
            placeholder="0.035 = 3.5%"
          />
          <FieldError text={errors.monthly_rate} />
        </div>
      </div>

      {form.debt_type === "installment" ? (
        <div>
          <label className="mb-1 block text-sm font-medium text-ink">Мінімальний платіж, грн</label>
          <input
            type="number"
            step="0.01"
            value={form.min_payment}
            onChange={(e) => setForm({ ...form, min_payment: e.target.value })}
            className={`${INPUT_CLASS} font-mono ${border("min_payment")}`}
          />
          <FieldError text={errors.min_payment} />
        </div>
      ) : (
        <>
          <p className="text-xs text-ink/40">Мінімальний платіж рахується автоматично (4% від балансу, мін. 200 грн)</p>
          <FieldError text={errors.min_payment} />
        </>
      )}
    </>
  );
}

function AffordabilityPanel({ data, extraValue }) {
  const hasSummary = data.count_ge_extra !== null && data.count_ge_extra !== undefined;
  const maxAbs = Math.max(1, ...data.months.map((m) => Math.abs(m.fcf_uah)));
  return (
    <div className="space-y-3 text-sm">
      {data.data_stale && (
        <p className="rounded-lg bg-gold/15 px-3 py-2 text-xs text-ink/80">
          ⚠ Дані застарілі: останні повні місяці з транзакціями давно минули — синхронізуй або
          імпортуй транзакції.
        </p>
      )}

      {hasSummary ? (
        <>
          <p className="text-ink">
            У <span className="font-semibold">{data.count_ge_extra}</span> із{" "}
            <span className="font-semibold">{data.n_months_used}</span> місяців вільний грошовий
            потік (після обов'язкових платежів) був не менший за {formatUah(extraValue)}.
          </p>
          <dl className="grid grid-cols-3 gap-2 text-xs">
            {[
              ["Медіана", data.median],
              ["Мінімум", data.min],
              ["Максимум", data.max],
            ].map(([label, value]) => (
              <div key={label} className="min-w-0">
                <dt className="text-ink/50">{label}</dt>
                <dd className="whitespace-nowrap font-mono text-ink">{formatUah(value)}</dd>
              </div>
            ))}
          </dl>
          <ul className="space-y-1">
            {data.months.map((m) => {
              const below = m.fcf_uah < extraValue;
              return (
                <li key={m.month} className="flex items-center gap-2 text-xs">
                  <span className="w-14 shrink-0 font-mono text-ink/60">{formatMonth(m.month)}</span>
                  <span className="h-2 min-w-0 flex-1 rounded bg-ink/5">
                    <span
                      className={`block h-2 rounded ${below ? "bg-brick/60" : "bg-sage/70"}`}
                      style={{ width: `${(Math.abs(m.fcf_uah) / maxAbs) * 100}%` }}
                    />
                  </span>
                  <span className="shrink-0 whitespace-nowrap text-right font-mono text-ink sm:w-28">
                    {formatUah(m.fcf_uah)}
                  </span>
                  {/* На вузькому екрані — лише позначка, повний підпис з sm. */}
                  <span
                    className={`shrink-0 whitespace-nowrap sm:w-28 ${below ? "text-brick" : "text-ink/50"}`}
                    title={below ? "менше за суму" : "не менше"}
                  >
                    <span className="sm:hidden">{below ? "✗" : "✓"}</span>
                    <span className="hidden sm:inline">{below ? "✗ менше за суму" : "✓ не менше"}</span>
                  </span>
                </li>
              );
            })}
          </ul>
        </>
      ) : null}

      {data.explanation && <p className="text-xs text-ink/70">{data.explanation}</p>}
      <p className="text-xs font-medium text-ink/70">{data.disclaimer}</p>
    </div>
  );
}

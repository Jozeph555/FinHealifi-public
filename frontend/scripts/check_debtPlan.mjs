// Перевірка чистих функцій src/utils/debtPlan.js, planFact.js, numberInput.js, money.js і formatAmountKopecks з transactions.js без тестового фреймворку.
// Запуск з frontend/: npm run check:plan (або node scripts/check_debtPlan.mjs).
// Кожна перевірка — assert; перша невдала зупиняє скрипт з ненульовим кодом виходу.
import assert from "node:assert/strict";

import * as m from "../src/utils/debtPlan.js";
import * as pf from "../src/utils/planFact.js";
import * as num from "../src/utils/numberInput.js";
import * as money from "../src/utils/money.js";
import * as tx from "../src/utils/transactions.js";

// Intl для uk-UA ставить нерозривні пробіли між розрядами; порівнюємо зі звичайними.
const plain = (text) => String(text).replace(/[  ]/g, " ");
const keys = (view) => view.messages.map((x) => x.key);

// --- дати ---
assert.equal(m.formatDateUk("2026-11-04"), "4 листопада 2026");
assert.equal(m.formatDateUk("2026-11-01"), "1 листопада 2026");
assert.equal(m.formatDateUk("2026-12"), "1 грудня 2026");
assert.equal(m.formatDateUk("2027-01-31"), "31 січня 2027");
assert.equal(m.formatDateUk(null), "—");
assert.equal(m.formatDateUk("2026-13-01"), "2026-13-01");
assert.equal(m.todayKyiv(new Date("2026-11-30T21:30:00Z")), "2026-11-30");
assert.equal(m.todayKyiv(new Date("2026-11-30T22:30:00Z")), "2026-12-01");

// --- версії плану ---
const schedule = [
  { month: "2026-11", debts: [
    { debt_id: "a", name: "Позика", payment: 3000, closing: 33750 },
    { debt_id: "b", name: "Картка", payment: 0, closing: 0 },
    { debt_id: "c", name: "Кредит", payment: 5000, closing: 1000 },
  ] },
];
const v1 = { version_id: "v1", source_plan_id: "p1", effective_from: "2026-11-01", materialized: true,
  extra_monthly: 1000, min_payments_total: 2000, total_monthly_payment: 3000, structure_changed: false, schedule };
const v2 = { version_id: "v2", source_plan_id: "p2", effective_from: "2026-12-01", materialized: false,
  extra_monthly: 2000, min_payments_total: null, total_monthly_payment: null, structure_changed: false, schedule: [] };
const onlyV1 = { ...v1, effective: v1, pending: null };
const v1AndV2 = { ...v2, effective: { ...v1, status: "superseded" }, pending: v2 };

// --- блок прийнятого плану ---
assert.equal(m.acceptedPlanView(null, "2026-11-04"), null);

let view = m.acceptedPlanView(onlyV1, "2026-11-04");
assert.equal(view.shown.version_id, "v1");
assert.deepEqual(view.flags, { notYetInForce: false, structureChanged: false });
assert.deepEqual(view.messages, [{ key: "in_force", text: "Діє з 1 листопада 2026" }]);

view = m.acceptedPlanView(v1AndV2, "2026-11-20");
assert.equal(view.shown.version_id, "v1", "суми — з чинної версії, не з очікуваної");
assert.equal(view.pending.version_id, "v2");
assert.deepEqual(keys(view), ["in_force", "pending"]);
assert.equal(plain(view.messages[1].text), "Новий план (додатковий платіж 2 000 грн) почне діяти з 1 грудня 2026.");

// «Ще не діє» і «склад змінився» одночасно: обидва повідомлення.
const v2Changed = { ...v2, structure_changed: true };
view = m.acceptedPlanView({ ...v2Changed, effective: { ...v1, structure_changed: true }, pending: v2Changed }, "2026-11-25");
assert.deepEqual(view.flags, { notYetInForce: true, structureChanged: true });
assert.deepEqual(keys(view), ["in_force", "pending", "structure_changed"]);
assert.equal(view.messages[2].text, m.STRUCTURE_CHANGED_NOTE);

// Склад змінився лише відносно очікуваної версії — повідомлення все одно є.
view = m.acceptedPlanView({ ...v2Changed, effective: v1, pending: v2Changed }, "2026-11-25");
assert.deepEqual(keys(view), ["in_force", "pending", "structure_changed"]);

// Версія без effective/pending у відповіді (старий формат): суми з неї самої.
view = m.acceptedPlanView(v1, "2026-11-04");
assert.equal(view.shown.version_id, "v1");
assert.deepEqual(keys(view), []);

// Склад розкладу відрізняється від прийнятої чернетки: нейтральні речення після «Діє з».
const v2Fixed = { ...v2, materialized: true, effective_from: "2026-12-01", schedule,
  added_debts: [{ debt_id: "n", name: "Новий борг" }], removed_debts: [{ debt_id: "b", name: "Картка" }] };
view = m.acceptedPlanView({ ...v2Fixed, effective: v2Fixed, pending: null }, "2026-12-07");
assert.deepEqual(keys(view), ["in_force", "added_debt:n", "removed_debt:b"]);
assert.equal(view.messages[1].text,
  "У план включено борг «Новий борг», якого не було в прийнятій версії. " +
  "Якщо хочеш інший розподіл, побудуй і прийми новий план.");
assert.equal(view.messages[2].text, "З плану прибрано борг «Картка», бо його закрито або деактивовано.");
// null (розкладу ще немає або чернетка без стартових залишків) — без речень.
view = m.acceptedPlanView({ ...v1, added_debts: null, removed_debts: null, effective: v1, pending: null }, "2026-11-04");
assert.deepEqual(keys(view), ["in_force"]);

// --- повідомлення про склад боргів: чинна (effective) і остання прийнята (pending) окремо ---
const effV = { ...v1, status: "superseded", structure_changed: false };
const penV = { ...v2, structure_changed: false };
const covered = "Борги, додані після прийняття діючого плану, увійдуть у план з 1 грудня 2026: новий план їх враховує.";
// 1. Нічого не змінилося — повідомлення немає.
assert.equal(m.structureNotice({ ...penV, effective: effV, pending: penV }), null);
view = m.acceptedPlanView({ ...penV, effective: effV, pending: penV }, "2026-11-20");
assert.ok(!keys(view).some((k) => k.startsWith("structure")));
// 2. Чинна розходиться, очікувана вже відповідає складу — борги увійдуть з дати очікуваної.
let notice = m.structureNotice({ ...penV, effective: { ...effV, structure_changed: true }, pending: penV });
assert.deepEqual(notice, { key: "structure_covered_by_pending", text: covered });
view = m.acceptedPlanView({ ...penV, effective: { ...effV, structure_changed: true }, pending: penV }, "2026-11-20");
assert.deepEqual(keys(view), ["in_force", "pending", "structure_covered_by_pending"]);
assert.equal(view.flags.structureChanged, false, "остання прийнята версія відповідає складу");
// 3. Остання прийнята (очікувана) розходиться — пропозиція побудувати новий план.
notice = m.structureNotice({ ...penV, structure_changed: true, effective: { ...effV, structure_changed: true },
  pending: { ...penV, structure_changed: true } });
assert.deepEqual(notice, { key: "structure_changed", text: m.STRUCTURE_CHANGED_NOTE });
// 3а. Очікуваної немає — дивимося на чинну.
notice = m.structureNotice({ ...v1, structure_changed: true, effective: { ...v1, structure_changed: true }, pending: null });
assert.deepEqual(notice, { key: "structure_changed", text: m.STRUCTURE_CHANGED_NOTE });
assert.equal(m.structureNotice({ ...v1, effective: v1, pending: null }), null);
// Старий формат без effective/pending — за самою версією.
assert.deepEqual(m.structureNotice({ ...v1, structure_changed: true }), { key: "structure_changed", text: m.STRUCTURE_CHANGED_NOTE });
assert.equal(m.structureNotice(null), null);

// --- платежі поточного місяця ---
assert.deepEqual(m.currentMonthPayments(v1, "2026-11-20"), [
  { debt_id: "c", name: "Кредит", payment: 5000, closing: 1000 },
  { debt_id: "a", name: "Позика", payment: 3000, closing: 33750 },
]);
assert.equal(m.currentMonthPayments(v1, "2026-12-03"), null);
assert.equal(m.currentMonthPayments(v2, "2026-12-03"), null);

// --- блок чернетки ---
const fresh = { plan_id: "p3", extra_monthly_uah: 1500, min_payments_total_uah: 2000, total_monthly_payment_uah: 3500 };
assert.equal(m.draftView(null, onlyV1), null);
assert.equal(m.draftView({ plan_id: "p1" }, onlyV1), null, "прийнята чернетка — без окремого блоку");
assert.equal(m.draftView({ plan_id: "p2" }, v1AndV2), null);

let draft = m.draftView(fresh, null);
assert.equal(draft.status, "ok");
assert.deepEqual(draft.notes, [m.DRAFT_NOTE]);
assert.deepEqual(draft.totals, { min: 2000, extra: 1500, total: 3500 });
assert.equal(draft.canAccept, true);

draft = m.draftView({ ...fresh, balances_changed: false, structure_changed: false }, onlyV1);
assert.deepEqual(draft.notes, [m.DRAFT_NOTE, m.DRAFT_KEEPS_ACCEPTED_NOTE]);

// Перший план: будь-яка зміна — як зміна складу.
draft = m.draftView({ ...fresh, balances_changed: true, structure_changed: false }, null);
assert.equal(draft.status, "structure_changed");
assert.equal(draft.canAccept, false);
assert.equal(draft.showNumbers, false);
assert.deepEqual(draft.totals, { min: null, extra: null, total: null });

// Другий і наступні: лише залишки — можна прийняти, числа з підписом.
draft = m.draftView({ ...fresh, balances_changed: true, structure_changed: false }, onlyV1);
assert.equal(draft.status, "balances_changed");
assert.equal(draft.canAccept, true);
assert.equal(draft.showNumbers, true);
assert.equal(draft.numbersCaption, "за залишками на момент побудови");
assert.deepEqual(draft.notes, [m.DRAFT_BALANCES_CHANGED_NOTE, m.DRAFT_KEEPS_ACCEPTED_NOTE]);
assert.deepEqual(draft.totals, { min: null, extra: 1500, total: null });

draft = m.draftView({ ...fresh, balances_changed: false, structure_changed: true }, onlyV1);
assert.equal(draft.status, "structure_changed");
assert.deepEqual(draft.notes, [m.DRAFT_STRUCTURE_CHANGED_NOTE]);
assert.equal(draft.canAccept, false);

// --- фіксація ---
assert.equal(m.materializeAction(null, "2026-12-07"), "none");
assert.equal(m.materializeAction(v1, "2026-12-07"), "none");
assert.equal(m.materializeAction(v2, "2026-11-20"), "none");
assert.equal(m.materializeAction(v2, "2026-12-03"), "too_early");
assert.equal(m.materializeAction(v2, "2026-12-05"), "too_early");
assert.equal(m.materializeAction(v2, "2026-12-06"), "auto");
assert.equal(m.materializeAction(v2, "2027-01-02"), "too_early");
assert.equal(
  m.materializeTooEarlyText(v2),
  "План почне діяти з 1 грудня 2026; зафіксувати можна з 6-го числа або за кнопкою «Зафіксувати попередній місяць».",
);

// --- тексти помилок ---
assert.equal(m.acceptErrorText(409, "будь-що"), "Борги змінилися після побудови плану. Побудуй план ще раз.");
assert.equal(m.acceptErrorText(503, "x"), "Розрахунок не встиг. Спробуй ще раз пізніше.");
assert.equal(m.acceptErrorText(422, "План неможливий."), "План неможливий.");
assert.equal(m.acceptErrorText(undefined, null), "Не вдалося прийняти план.");
assert.equal(m.materializeErrorText(409, "Внеси платежі."), "Внеси платежі.");
assert.equal(m.materializeErrorText(503, "x"), "Розрахунок не встиг. Спробуй ще раз пізніше.");

// --- суми на місяць ---
assert.deepEqual(m.draftTotals(fresh), { min: 2000, extra: 1500, total: 3500 });
assert.deepEqual(m.versionTotals(v2), { min: null, extra: 2000, total: null });

// --- план на Dashboard ---
const cmp = { milp: { months_to_payoff: 21, total_interest_paid: 48210 },
  avalanche: { months_to_payoff: 21, total_interest_paid: 66000 } };
const dashDraft = { plan_id: "p9", months_to_payoff: 21, total_interest_paid: 48210, horizon_months: null, comparison: cmp };
const firstCmp = { milp: { months_to_payoff: 23, total_interest_paid: 45930 },
  avalanche: { months_to_payoff: 23, total_interest_paid: 61500 } };
const firstV = { version_id: "v1", source_plan_id: "p9", start_kind: "acceptance_balances", effective_from: "2026-11-01",
  materialized: true, months_to_payoff: 23, total_interest: 45930, comparison: firstCmp,
  comparison_based_on: "balances_at_acceptance" };
assert.equal(m.dashboardPlanView(null, null), null);

let dash = m.dashboardPlanView(null, dashDraft);
assert.equal(dash.kind, "draft");
assert.equal(dash.caption, "Чернетка, ще не прийнята");
assert.equal(dash.monthsToPayoff, 21);
assert.equal(dash.totalInterest, 48210);
assert.equal(dash.comparison, cmp);
assert.equal(dash.comparisonCaption, null);

// Є прийнятий план — числа й порівняння з чинної версії, навіть якщо остання чернетка інша.
dash = m.dashboardPlanView({ ...firstV, effective: firstV, pending: null }, { ...dashDraft, plan_id: "p10" });
assert.equal(dash.kind, "accepted");
assert.equal(dash.caption, "Прийнятий план, діє з 1 листопада 2026");
assert.equal(dash.monthsToPayoff, 23);
assert.equal(dash.totalInterest, 45930);
assert.equal(dash.pendingText, null);
assert.equal(dash.comparison, firstCmp, "порівняння з вихідної чернетки версії, а не з останньої");
assert.equal(dash.comparisonCaption, "за залишками на момент прийняття");
assert.equal(dash.comparisonNote, null);

// Перша версія без comparison у відповіді (вихідну чернетку не знайдено) — пояснення.
dash = m.dashboardPlanView({ ...firstV, comparison: null, effective: { ...firstV, comparison: null }, pending: null }, dashDraft);
assert.equal(dash.comparison, null);
assert.equal(dash.comparisonNote, m.DASHBOARD_COMPARISON_OTHER_DRAFT_NOTE);

// Чинна перша версія і очікувана друга: числа з чинної, окремий рядок про нову.
const secondV = { version_id: "v2", source_plan_id: "p10", start_kind: "month_start_actual",
  effective_from: "2026-12-01", materialized: false, months_to_payoff: null, total_interest: null };
dash = m.dashboardPlanView({ ...secondV, effective: { ...firstV, status: "superseded" }, pending: secondV },
  { ...dashDraft, plan_id: "p10" });
assert.equal(dash.caption, "Прийнятий план, діє з 1 листопада 2026");
assert.equal(dash.pendingText, "Новий план почне діяти з 1 грудня 2026.");

// --- очікувана версія з орієнтовними числами чернетки ---
const pendingWithPreview = { ...secondV, extra_monthly: 1500,
  preview: { months_to_payoff: 20, total_interest: 39870, horizon_months: null, based_on: "balances_at_build" } };
assert.equal(plain(m.pendingPlanText(pendingWithPreview)),
  "Новий план (додатковий платіж 1 500 грн) почне діяти з 1 грудня 2026: " +
  "орієнтовно 20 міс., переплата 39 870 грн (за залишками на момент побудови)");
assert.equal(plain(m.pendingPlanText({ ...pendingWithPreview,
  preview: { months_to_payoff: null, total_interest: 90000, horizon_months: 36, based_on: "balances_at_build" } })),
  "Новий план (додатковий платіж 1 500 грн) почне діяти з 1 грудня 2026: " +
  "за розрахунком не погашається за 36 міс. (за залишками на момент побудови)");
// Зафіксована версія — без орієнтовних чисел.
assert.equal(plain(m.pendingPlanText({ ...pendingWithPreview, materialized: true })),
  "Новий план (додатковий платіж 1 500 грн) почне діяти з 1 грудня 2026.");
// Той самий текст на Dashboard і на «Боргах».
const withPreview = { ...pendingWithPreview, effective: { ...firstV, status: "superseded" }, pending: pendingWithPreview };
assert.equal(m.dashboardPlanView(withPreview, null).pendingText, m.pendingPlanText(pendingWithPreview));
assert.equal(m.acceptedPlanView(withPreview, "2026-11-20").messages.find((x) => x.key === "pending").text,
  m.pendingPlanText(pendingWithPreview));

// --- підпис блоку з числами чернетки на «Боргах» ---
assert.equal(m.draftDetailsCaption(null, null), null);
assert.equal(m.draftDetailsCaption({ plan_id: "p11" }, null), "Чернетка");
assert.equal(m.draftDetailsCaption({ plan_id: "p11" }, withPreview), "Чернетка", "ще не прийнята");
assert.equal(m.draftDetailsCaption({ plan_id: "p10" }, withPreview),
  "Останній побудований план, очікує початку дії з 1 грудня 2026");
assert.equal(m.draftDetailsCaption({ plan_id: "p9" }, withPreview),
  "Розрахунок прийнятого плану, діє з 1 листопада 2026");
assert.equal(m.draftDetailsCaption({ plan_id: "p9" }, { ...firstV, effective: firstV, pending: null }),
  "Розрахунок прийнятого плану, діє з 1 листопада 2026");
assert.equal(dash.monthsToPayoff, 23);

// Чинна версія від залишків на початок місяця: порівняння сховано навіть із «її» чернеткою.
const secondFixed = { ...secondV, materialized: true, months_to_payoff: 20, total_interest: 50000 };
dash = m.dashboardPlanView({ ...secondFixed, effective: secondFixed, pending: null }, { ...dashDraft, plan_id: "p10" });
assert.equal(dash.comparison, null);
assert.equal(dash.comparisonNote, m.DASHBOARD_COMPARISON_MONTH_START_NOTE);
assert.equal(dash.caption, "Прийнятий план, діє з 1 грудня 2026");
// Версія ще не зафіксована: місяців і переплати немає.
dash = m.dashboardPlanView({ ...secondV, effective: secondV, pending: null }, null);
assert.equal(dash.materialized, false);
assert.equal(dash.monthsToPayoff, null);

// === planFact.js ===============================================================================

// --- суми ---
// Єдиний формат сум (utils/money.js): цілі гривні; |сума| < 1 грн і не нуль — дві цифри після коми.
assert.equal(plain(money.formatUah(41276.55)), "41 277 грн");
assert.equal(plain(money.formatUah(0.4)), "0,40 грн", "копійки не округлюються до 0 грн");
assert.equal(money.formatUah(0), "0 грн");
assert.equal(plain(money.formatUah(-500)), "−500 грн");
assert.equal(plain(money.formatUah(-0.4)), "−0,40 грн");
assert.equal(plain(money.formatUah(1234.5)), "1 235 грн", "half-up за модулем");
assert.equal(plain(money.formatUah(-1234.5)), "−1 235 грн");
assert.equal(plain(money.formatUah(0.996)), "1 грн");
assert.equal(money.formatUah(0.004), "0 грн");
assert.equal(plain(money.formatUah(1234567)), "1 234 567 грн");
assert.ok(/^1[\u00a0\u202f]500 грн$/.test(money.formatUah(1500)), "роздільник розрядів uk-UA — нерозривний пробіл");
assert.equal(plain(money.formatUah(45000, { withSign: true })), "+45 000 грн");
assert.equal(money.formatUah(0, { withSign: true }), "0 грн");
assert.equal(money.formatUah(null), "—");
assert.equal(money.formatUah(NaN), "—");
// Ті самі суми в усіх модулях: платіж плану, план-факт, транзакції в копійках.
assert.equal(plain(pf.checkCaption({ as_of: "2026-11-20", balance: 41276.55 })),
  "Звірка з випискою: 41 277 грн на 20 листопада 2026");
assert.equal(plain(tx.formatAmountKopecks(-4127655)), "−41 277 грн");
assert.equal(plain(tx.formatAmountKopecks(4500000)), "+45 000 грн");
assert.equal(plain(tx.formatAmountKopecks(-40)), "−0,40 грн");
assert.equal(plain(tx.formatAmountKopecks(-123455700, { withSign: false })), "1 234 557 грн");

assert.deepEqual(pf.parseMoneyInput("1500"), { value: 1500, error: null });
assert.deepEqual(pf.parseMoneyInput("1 500,5"), { value: 1500.5, error: null });
assert.equal(pf.parseMoneyInput("0").error, "Сума має бути більшою за 0.");
assert.deepEqual(pf.parseMoneyInput("0", { allowZero: true }), { value: 0, error: null });
assert.equal(pf.parseMoneyInput("-5").error, "Сума не може бути від'ємною.");
assert.ok(pf.parseMoneyInput("1e5").error);
assert.ok(pf.parseMoneyInput("").error);

// --- місяці: межа за Києвом ---
const kyivMonth = (iso, months) => pf.defaultMonth(months, m.todayKyiv(new Date(iso)));
const novDec = [{ month: "2026-11" }, { month: "2026-12" }];
assert.equal(kyivMonth("2026-11-30T21:59:00Z", novDec), "2026-11", "30.11 23:59 за Києвом — ще листопад");
assert.equal(kyivMonth("2026-11-30T22:00:00Z", novDec), "2026-12", "01.12 00:00 за Києвом — уже грудень");
const marApr = [{ month: "2027-03" }, { month: "2027-04" }];
assert.equal(kyivMonth("2027-03-31T20:59:00Z", marApr), "2027-03", "літній час: UTC+3");
assert.equal(kyivMonth("2027-03-31T21:00:00Z", marApr), "2027-04");
assert.equal(pf.formatMonthUk("2026-11"), "Листопад 2026");
const pfMonths = [{ month: "2026-11" }, { month: "2026-12" }];
assert.equal(pf.defaultMonth(pfMonths, "2026-12-03"), "2026-12");
assert.equal(pf.defaultMonth(pfMonths, "2027-01-02"), "2026-12");
assert.equal(pf.defaultMonth([], "2026-12-03"), null);

// --- помилки платежу ---
assert.equal(
  pf.paymentErrorText(409, "Платіж раніше дати прийняття плану (14.11.2026) не входить у порівняння."),
  "Платіж раніше за дату прийняття плану, тому не враховується.",
);
assert.equal(
  pf.paymentErrorText(409, "Місяць 2026-11 закрито: він належить попередній версії плану."),
  "Місяць 2026-11 закрито: він належить попередній версії плану.",
);
assert.equal(pf.paymentErrorText(422, "Дата не може бути в майбутньому."), "Дата не може бути в майбутньому.");
assert.equal(
  pf.paymentErrorText(422, [{ loc: ["body", "amount"], msg: "Input should be greater than 0" }]),
  "Сума має бути більшою за 0.",
);
assert.equal(pf.paymentErrorText(500, null), "Не вдалося зберегти платіж (HTTP 500).");

// --- стан місяця ---
const openMonth = { month: "2026-12", closed: false, entry_state: "entered", counted_from: null };
assert.deepEqual(pf.monthStateLabels(openMonth), ["триває"]);
assert.deepEqual(pf.monthStateLabels({ ...openMonth, entry_state: "not_entered" }), ["триває", "платежів не внесено"]);
const closedMonth = { month: "2026-11", closed: true, entry_state: "entered", counted_from: "2026-11-04" };
assert.deepEqual(pf.monthStateLabels(closedMonth), ["закрито"]);
assert.deepEqual(pf.monthStateLabels({ ...closedMonth, entry_state: "not_entered" }), ["платежів не внесено"]);
assert.deepEqual(pf.monthStateLabels({ ...closedMonth, entry_state: "no_payments_confirmed" }),
  ["закрито", "позначено: платежів не було"]);
assert.equal(pf.countedFromText(closedMonth), "Враховано платежі з 4 листопада 2026");
assert.equal(pf.countedFromText(openMonth), null);

// --- різниці ---
assert.equal(plain(pf.paymentDiffText(-500)), "внесено на 500 грн менше за план");
assert.equal(plain(pf.paymentDiffText(300)), "внесено на 300 грн більше за план");
assert.equal(plain(pf.paymentDiffText(-0.4)), "внесено на 0,40 грн менше за план");
assert.equal(pf.paymentDiffText(0.004), "внесено стільки, скільки в плані");
assert.equal(pf.paymentDiffText(null), null);
assert.equal(plain(pf.balanceDiffText(250)), "залишок на 250 грн більший за плановий");
assert.equal(plain(pf.balanceDiffText(-250)), "залишок на 250 грн менший за плановий");

// Місяць, що триває: замість різниці — скільки ще лишилося внести, без від'ємної різниці.
const openRow = { planned_payment: 3000, actual_payment: 1000, payment_diff: -2000, remaining_to_plan: 2000,
  planned_closing: 7000, actual_closing: 9000 };
let rowView = pf.planFactRowView(openRow, openMonth);
assert.equal(plain(rowView.diff), "Ще лишилося внести за планом: 2 000 грн");
assert.ok(!rowView.diff.includes("менше"), "стан «триває» не показує різниці з планом");
rowView = pf.planFactRowView({ ...openRow, actual_payment: null, remaining_to_plan: 3000 },
  { ...openMonth, entry_state: "not_entered" });
assert.equal(plain(rowView.diff), "Ще лишилося внести за планом: 3 000 грн");
assert.equal(rowView.actual, "—");
rowView = pf.planFactRowView({ ...openRow, actual_payment: 3500, payment_diff: 500, remaining_to_plan: 0 }, openMonth);
assert.equal(plain(rowView.diff), "Ще лишилося внести за планом: 0 грн");
// Закритий місяць: описова різниця; not_entered — «—».
rowView = pf.planFactRowView({ ...openRow, remaining_to_plan: null }, closedMonth);
assert.equal(plain(rowView.diff), "внесено на 2 000 грн менше за план");
assert.equal(plain(rowView.planned), "3 000 грн");
rowView = pf.planFactRowView({ ...openRow, actual_payment: null, payment_diff: null, actual_closing: null,
  remaining_to_plan: null }, { ...closedMonth, entry_state: "not_entered" });
assert.equal(rowView.diff, "—");
assert.equal(rowView.actualClosing, "немає даних", "фактичний залишок без внесених платежів");
assert.equal(rowView.balance, null);
// Закритий місяць без звірки: різниця залишків — другорядним реченням.
assert.equal(plain(pf.planFactRowView({ ...openRow, remaining_to_plan: null, balance_diff: 2000, check: null },
  closedMonth).balance), "залишок на 2 000 грн більший за плановий");
// Відкритий місяць: різниця залишків не показується.
assert.equal(pf.planFactRowView({ ...openRow, balance_diff: 2000, check: null }, openMonth).balance, null);
// Рядок зі звіркою: замість різниці залишків — пояснення; різниця платежів лишається.
const checkRow = { planned_payment: 300, actual_payment: 0, payment_diff: -300, remaining_to_plan: null,
  planned_closing: 7420, actual_closing: 7500, balance_diff: null,
  check: { as_of: "2026-10-20", balance: 7500, source: "statement" } };
rowView = pf.planFactRowView(checkRow, closedMonth);
assert.equal(rowView.balance,
  "Після звірки відсотки решти місяця не враховано, тому залишок не порівнюється з плановим");
assert.equal(plain(rowView.diff), "внесено на 300 грн менше за план");
assert.equal(plain(rowView.actualClosing), "7 500 грн");
assert.equal(pf.planFactRowView(checkRow, openMonth).balance, pf.CHECK_BALANCE_NOTE);
assert.equal(pf.planFactRowView({ ...openRow, planned_payment: null }, closedMonth).diff, "немає в плані на цей місяць");

// --- підсумок по закритих місяцях ---
assert.equal(pf.cumulativeView({ debts: [], payment_diff: 0, balance_diff: null }), null);
const cum = pf.cumulativeView({
  debts: [{ debt_id: "a", name: "Позика", payment_diff: -500, balance_diff: 510 },
          { debt_id: "b", name: "Картка", payment_diff: 0, balance_diff: null }],
  payment_diff: -500, balance_diff: 510,
});
assert.equal(plain(cum.payment), "внесено на 500 грн менше за план");
assert.equal(plain(cum.balance), "залишок на 510 грн більший за плановий");
assert.equal(cum.debts[1].balance, null);

// --- позначка «платежів не було» ---
assert.equal(pf.noPaymentsAction({ ...closedMonth, entry_state: "not_entered", editable: true }), "mark");
assert.equal(pf.noPaymentsAction({ ...closedMonth, entry_state: "no_payments_confirmed", editable: true }), "unmark");
assert.equal(pf.noPaymentsAction({ ...closedMonth, entry_state: "entered", editable: true }), null);
assert.equal(pf.noPaymentsAction({ ...closedMonth, entry_state: "not_entered", editable: false }), null,
  "заблокований місяць — без дій");

// --- звірка ---
assert.equal(pf.defaultCheckDate("2026-12", "2026-12-07"), "2026-12-07");
assert.equal(pf.defaultCheckDate("2026-11", "2026-12-07"), "2026-11-30");
assert.equal(pf.defaultCheckDate("2027-02", "2027-03-02"), "2027-02-28");
assert.equal(pf.defaultCheckDate("2028-02", "2028-03-02"), "2028-02-29");
assert.equal(plain(pf.checkCaption({ as_of: "2026-11-20", balance: 8765.43, source: "statement" })),
  "Звірка з випискою: 8 765 грн на 20 листопада 2026");
assert.equal(pf.checkCaption(null), null);
assert.equal(pf.BALANCE_CHECK_NOTE, "Від цієї дати залишок рахується за випискою.");

// --- жодних оцінок у згенерованих реченнях ---
const generated = [
  ...[-500, -0.4, 0, 300].map(pf.paymentDiffText),
  ...[-250, 0, 250].map(pf.balanceDiffText),
  pf.remainingToPlanText(2000), pf.NOT_ENTERED_NOTE, pf.PAYMENT_BEFORE_PLAN_TEXT,
  ...[openMonth, closedMonth, { ...closedMonth, entry_state: "not_entered" },
      { ...closedMonth, entry_state: "no_payments_confirmed" }].flatMap(pf.monthStateLabels),
  ...Object.values(pf.planFactRowView(openRow, openMonth)),
  ...Object.values(pf.planFactRowView({ ...openRow, remaining_to_plan: null }, closedMonth)),
  ...Object.values(pf.planFactRowView(checkRow, closedMonth)), pf.NO_DATA_TEXT,
  cum.payment, cum.balance, pf.NO_PAYMENTS_BUTTON_LABEL, pf.NO_PAYMENTS_UNDO_LABEL,
  pf.BALANCE_CHECK_LABEL, pf.BALANCE_CHECK_NOTE, pf.checkCaption({ as_of: "2026-11-20", balance: 1 }),
];
for (const text of generated) {
  for (const word of pf.FORBIDDEN_WORDS) {
    assert.ok(!String(text).toLowerCase().includes(word), `«${word}» у реченні «${text}»`);
  }
}

// === numberInput.js ============================================================================
assert.equal(num.normalizeIntegerInput("012"), "12", "початковий нуль прибирається");
assert.equal(num.normalizeIntegerInput("0"), "0");
assert.equal(num.normalizeIntegerInput("00"), "0");
assert.equal(num.normalizeIntegerInput(""), "", "порожнє значення під час набору дозволено");
assert.equal(num.normalizeIntegerInput("1a2-"), "12");
assert.equal(num.clampInteger("", 1, 24, 1), 1);
assert.equal(num.clampInteger("0", 1, 24, 1), 1);
assert.equal(num.clampInteger("012", 1, 24, 1), 12);
assert.equal(num.clampInteger("99", 1, 24, 1), 24);

console.log("debtPlan.js, planFact.js, numberInput.js і money.js: усі перевірки пройдено");

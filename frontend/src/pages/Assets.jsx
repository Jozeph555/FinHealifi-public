import { useState, useEffect } from "react";
import api from "../services/api";

function formatMoney(value) {
  if (value === null || value === undefined) return "—";
  return new Intl.NumberFormat("uk-UA", { maximumFractionDigits: 0 }).format(value) + " грн";
}

const FALLBACK_ASSET_TYPES = ["Депозит", "Інвестиції", "Нерухомість", "Готівка", "Інше"];
const CUSTOM_TYPE_VALUE = "__custom__";
const LIQUIDITY_LABELS = { high: "Висока", medium: "Середня", low: "Низька" };

const emptyForm = {
  name: "",
  asset_type: "Депозит",
  custom_type: "",
  current_amount: "",
  liquidity_level: "medium",
  liquidity_days: "",
  annual_rate: "",
  rate_note: "",
  opened_at: "",
};

function buildPayload(f) {
  const resolvedType = f.asset_type === CUSTOM_TYPE_VALUE ? f.custom_type : f.asset_type;
  return {
    name: f.name,
    asset_type: resolvedType,
    current_amount: Number(f.current_amount),
    liquidity_level: f.liquidity_level,
    liquidity_days: f.liquidity_days ? Number(f.liquidity_days) : null,
    annual_rate: f.annual_rate ? Number(f.annual_rate) : null,
    rate_note: f.rate_note || null,
    opened_at: f.opened_at || null,
  };
}

function AssetFormFields({ form, setForm, assetTypes }) {
  return (
    <>
      <div>
        <label className="mb-1 block text-sm font-medium text-ink">Назва</label>
        <input
          type="text"
          required
          value={form.name}
          onChange={(e) => setForm({ ...form, name: e.target.value })}
          className="w-full rounded-lg border border-ink/15 bg-white px-3 py-2 outline-none focus:border-forest focus:ring-1 focus:ring-forest"
          placeholder="напр. Депозит ПриватБанк"
        />
      </div>

      <div>
        <label className="mb-1 block text-sm font-medium text-ink">Тип активу</label>
        <select
          value={form.asset_type}
          onChange={(e) => setForm({ ...form, asset_type: e.target.value })}
          className="w-full rounded-lg border border-ink/15 bg-white px-3 py-2 outline-none focus:border-forest focus:ring-1 focus:ring-forest"
        >
          {assetTypes.map((t) => (
            <option key={t} value={t}>
              {t}
            </option>
          ))}
          <option value={CUSTOM_TYPE_VALUE}>✏️ Власний тип...</option>
        </select>
      </div>

      {form.asset_type === CUSTOM_TYPE_VALUE && (
        <div>
          <label className="mb-1 block text-sm font-medium text-ink">Свій тип активу</label>
          <input
            type="text"
            required
            value={form.custom_type}
            onChange={(e) => setForm({ ...form, custom_type: e.target.value })}
            className="w-full rounded-lg border border-ink/15 bg-white px-3 py-2 outline-none focus:border-forest focus:ring-1 focus:ring-forest"
            placeholder="напр. Криптовалюта"
          />
        </div>
      )}

      <div>
        <label className="mb-1 block text-sm font-medium text-ink">Поточна сума, грн</label>
        <input
          type="number"
          step="1"
          required
          value={form.current_amount}
          onChange={(e) => setForm({ ...form, current_amount: e.target.value })}
          className="w-full rounded-lg border border-ink/15 bg-white px-3 py-2 font-mono outline-none focus:border-forest focus:ring-1 focus:ring-forest"
        />
      </div>

      <div className="grid grid-cols-2 gap-3">
        <div>
          <label className="mb-1 block text-sm font-medium text-ink">Ліквідність</label>
          <select
            value={form.liquidity_level}
            onChange={(e) => setForm({ ...form, liquidity_level: e.target.value })}
            className="w-full rounded-lg border border-ink/15 bg-white px-3 py-2 outline-none focus:border-forest focus:ring-1 focus:ring-forest"
          >
            <option value="high">Висока</option>
            <option value="medium">Середня</option>
            <option value="low">Низька</option>
          </select>
        </div>
        <div>
          <label className="mb-1 block text-sm font-medium text-ink">Днів до доступу</label>
          <input
            type="number"
            step="1"
            value={form.liquidity_days}
            onChange={(e) => setForm({ ...form, liquidity_days: e.target.value })}
            className="w-full rounded-lg border border-ink/15 bg-white px-3 py-2 font-mono outline-none focus:border-forest focus:ring-1 focus:ring-forest"
            placeholder="опційно"
          />
        </div>
      </div>

      <div className="grid grid-cols-2 gap-3">
        <div>
          <label className="mb-1 block text-sm font-medium text-ink">Ставка, % річних</label>
          <input
            type="number"
            step="0.1"
            value={form.annual_rate}
            onChange={(e) => setForm({ ...form, annual_rate: e.target.value })}
            className="w-full rounded-lg border border-ink/15 bg-white px-3 py-2 font-mono outline-none focus:border-forest focus:ring-1 focus:ring-forest"
            placeholder="опційно"
          />
        </div>
        <div>
          <label className="mb-1 block text-sm font-medium text-ink">Дата відкриття</label>
          <input
            type="date"
            value={form.opened_at}
            onChange={(e) => setForm({ ...form, opened_at: e.target.value })}
            className="w-full rounded-lg border border-ink/15 bg-white px-3 py-2 outline-none focus:border-forest focus:ring-1 focus:ring-forest"
          />
        </div>
      </div>

      <div>
        <label className="mb-1 block text-sm font-medium text-ink">Примітка про умови дохідності</label>
        <input
          type="text"
          value={form.rate_note}
          onChange={(e) => setForm({ ...form, rate_note: e.target.value })}
          className="w-full rounded-lg border border-ink/15 bg-white px-3 py-2 outline-none focus:border-forest focus:ring-1 focus:ring-forest"
          placeholder="опційно, напр. дострокове зняття без втрати відсотків"
        />
      </div>
    </>
  );
}

export default function Assets() {
  const [assets, setAssets] = useState([]);
  const [assetTypes, setAssetTypes] = useState(FALLBACK_ASSET_TYPES);
  const [isLoading, setIsLoading] = useState(true);

  const [form, setForm] = useState(emptyForm);
  const [formError, setFormError] = useState(null);
  const [isSubmitting, setIsSubmitting] = useState(false);

  const [editingId, setEditingId] = useState(null);
  const [editForm, setEditForm] = useState(null);
  const [editError, setEditError] = useState(null);
  const [isSavingEdit, setIsSavingEdit] = useState(false);

  async function loadAll() {
    setIsLoading(true);
    const [assetsRes, typesRes] = await Promise.allSettled([
      api.get("/assets"),
      api.get("/assets/types"),
    ]);

    if (assetsRes.status === "fulfilled") setAssets(assetsRes.value.data);
    if (typesRes.status === "fulfilled") setAssetTypes(typesRes.value.data.asset_types);

    setIsLoading(false);
  }

  useEffect(() => {
    loadAll();
  }, []);

  async function handleAddAsset(e) {
    e.preventDefault();
    setFormError(null);
    setIsSubmitting(true);
    try {
      await api.post("/assets", buildPayload(form));
      setForm(emptyForm);
      await loadAll();
    } catch (err) {
      setFormError(err.response?.data?.detail || "Не вдалося додати актив.");
    } finally {
      setIsSubmitting(false);
    }
  }

  async function handleArchive(id) {
    if (!confirm("Архівувати актив? Історія лишиться, але він зникне зі списку активних.")) {
      return;
    }
    try {
      await api.patch(`/assets/${id}`, { is_active: false });
      await loadAll();
    } catch (err) {
      alert(err.response?.data?.detail || "Не вдалося архівувати актив.");
    }
  }

  function startEdit(asset) {
    setEditError(null);
    const isKnownType = assetTypes.includes(asset.asset_type);
    setEditForm({
      name: asset.name,
      asset_type: isKnownType ? asset.asset_type : CUSTOM_TYPE_VALUE,
      custom_type: isKnownType ? "" : asset.asset_type,
      current_amount: String(asset.current_amount),
      liquidity_level: asset.liquidity_level,
      liquidity_days: asset.liquidity_days ?? "",
      annual_rate: asset.annual_rate ?? "",
      rate_note: asset.rate_note ?? "",
      opened_at: asset.opened_at || "",
    });
    setEditingId(asset.id);
  }

  function cancelEdit() {
    setEditingId(null);
    setEditForm(null);
    setEditError(null);
  }

  async function handleSaveEdit(e, id) {
    e.preventDefault();
    setEditError(null);
    setIsSavingEdit(true);
    try {
      await api.patch(`/assets/${id}`, buildPayload(editForm));
      cancelEdit();
      await loadAll();
    } catch (err) {
      setEditError(err.response?.data?.detail || "Не вдалося зберегти зміни.");
    } finally {
      setIsSavingEdit(false);
    }
  }

  if (isLoading) {
    return <p className="text-ink/50">Завантаження...</p>;
  }

  return (
    <div className="space-y-6">
      <h1 className="font-display text-2xl font-semibold text-forest">Активи</h1>

      {/* Список активних активів */}
      <section className="rounded-xl border border-ink/10 bg-white p-5">
        <h2 className="mb-4 text-sm font-medium uppercase tracking-wide text-ink/50">
          Активні активи
        </h2>
        {assets.length === 0 ? (
          <p className="text-sm text-ink/60">Ще немає жодного активу.</p>
        ) : (
          <div className="space-y-3">
            {assets.map((a) =>
              editingId === a.id ? (
                <form
                  key={a.id}
                  onSubmit={(e) => handleSaveEdit(e, a.id)}
                  className="space-y-3 rounded-lg border border-forest/30 bg-forest/5 p-3"
                >
                  <AssetFormFields form={editForm} setForm={setEditForm} assetTypes={assetTypes} />

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
                <div
                  key={a.id}
                  className="flex items-center justify-between rounded-lg border border-ink/10 p-3"
                >
                  <div>
                    <p className="font-medium text-ink">{a.name}</p>
                    <p className="text-xs text-ink/50">
                      {a.asset_type} • {LIQUIDITY_LABELS[a.liquidity_level] || a.liquidity_level} ліквідність
                      {a.liquidity_days != null ? `, ${a.liquidity_days} дн. до доступу` : ""}
                      {a.annual_rate != null ? ` • ${a.annual_rate}% річних` : ""}
                    </p>
                    {a.rate_note && <p className="text-xs text-ink/40">{a.rate_note}</p>}
                  </div>
                  <div className="flex items-center gap-3">
                    <span className="font-mono text-lg text-forest">{formatMoney(a.current_amount)}</span>
                    <button
                      onClick={() => startEdit(a)}
                      className="text-xs text-ink/40 hover:text-forest"
                    >
                      Редагувати
                    </button>
                    <button
                      onClick={() => handleArchive(a.id)}
                      className="text-xs text-ink/40 hover:text-brick"
                    >
                      Архівувати
                    </button>
                  </div>
                </div>
              )
            )}
          </div>
        )}
      </section>

      {/* Форма додавання активу */}
      <section className="rounded-xl border border-ink/10 bg-white p-5">
        <h2 className="mb-4 text-sm font-medium uppercase tracking-wide text-ink/50">
          Додати актив
        </h2>
        <form onSubmit={handleAddAsset} className="space-y-3">
          <AssetFormFields form={form} setForm={setForm} assetTypes={assetTypes} />

          {formError && (
            <p className="rounded-lg bg-brick/10 px-3 py-2 text-sm text-brick">{formError}</p>
          )}

          <button
            type="submit"
            disabled={isSubmitting}
            className="w-full rounded-lg bg-forest px-4 py-2.5 font-medium text-paper transition hover:bg-forest-light disabled:opacity-50"
          >
            {isSubmitting ? "Додаємо..." : "Додати актив"}
          </button>
        </form>
      </section>
    </div>
  );
}

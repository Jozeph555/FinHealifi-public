import { useState, useEffect } from "react";
import api from "../services/api";

const CUSTOM_BANK_VALUE = "__custom__";

export default function CsvImport({ onDone }) {
  const [file, setFile] = useState(null);
  const [preview, setPreview] = useState(null);
  const [error, setError] = useState(null);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [result, setResult] = useState(null);
  const [banks, setBanks] = useState([]);
  const [bankId, setBankId] = useState("");
  const [customBankName, setCustomBankName] = useState("");
  const [customBankMfo, setCustomBankMfo] = useState("");

  useEffect(() => {
    api
      .get("/banks")
      .then((res) => setBanks(res.data))
      .catch(() => setBanks([]));
  }, []);

  async function handlePreview(e) {
    e.preventDefault();
    if (!file) return;
    setError(null);
    setIsSubmitting(true);
    try {
      const formData = new FormData();
      formData.append("file", file);
      const res = await api.post("/import/statement/preview", formData, {
        headers: { "Content-Type": "multipart/form-data" },
      });
      setPreview(res.data);
    } catch (err) {
      setError(err.response?.data?.detail || "Не вдалося розпізнати файл.");
    } finally {
      setIsSubmitting(false);
    }
  }

  const isCustomBank = bankId === CUSTOM_BANK_VALUE;
  const hasBankSelected = isCustomBank ? customBankName.trim().length > 0 : bankId.length > 0;

  async function handleConfirm() {
    setError(null);
    setIsSubmitting(true);
    try {
      const res = await api.post("/import/statement/confirm", {
        import_id: preview.import_id,
        bank_id: isCustomBank ? null : bankId,
        custom_bank_name: isCustomBank ? customBankName : null,
        custom_bank_mfo: isCustomBank ? customBankMfo || null : null,
      });
      setResult(res.data);
    } catch (err) {
      setError(err.response?.data?.detail || "Не вдалося імпортувати транзакції.");
    } finally {
      setIsSubmitting(false);
    }
  }

  if (result) {
    return (
      <div className="space-y-4 text-center">
        <p className="font-display text-xl text-forest">Готово!</p>
        <p className="text-sm text-ink/70">
          Імпортовано {result.created} транзакцій
          {result.skipped > 0 && `, пропущено ${result.skipped}`}.
        </p>
        {result.duplicates_removed > 0 && (
          <p className="text-sm text-ink/70">
            З них {result.duplicates_removed} виявилися дублікатами вже наявних транзакцій —
            видалено автоматично.
          </p>
        )}
        <button
          onClick={onDone}
          className="w-full rounded-lg bg-gold px-4 py-2.5 font-medium text-ink transition hover:bg-gold-light"
        >
          Перейти до Dashboard
        </button>
      </div>
    );
  }

  if (preview) {
    return (
      <div className="space-y-4">
        <div>
          <p className="mb-2 text-sm font-medium text-ink">Визначені колонки:</p>
          <div className="rounded-lg border border-ink/10 bg-white p-3 text-sm">
            {Object.entries(preview.mapping).map(([field, col]) => (
              <div key={field} className="flex justify-between py-0.5">
                <span className="text-ink/50">{field}</span>
                <span className="font-mono text-ink">{col}</span>
              </div>
            ))}
          </div>
        </div>

        {preview.warnings?.length > 0 && (
          <div className="space-y-1.5">
            {preview.warnings.map((w, i) => (
              <p
                key={i}
                className={`rounded-lg px-3 py-2 text-sm ${
                  w.severity === "blocking" ? "bg-brick/10 text-brick" : "bg-gold/15 text-ink/80"
                }`}
              >
                {w.message}
              </p>
            ))}
          </div>
        )}

        {preview.page_diagnostics?.length > 0 && (
          <div>
            <p className="mb-1 text-xs font-medium text-ink/70">
              Сторінки з розбіжністю кількості колонок:
            </p>
            <div className="rounded-lg border border-ink/10 bg-white p-2 text-xs text-ink/70">
              {preview.page_diagnostics.map((d) => (
                <div key={d.page} className="flex justify-between py-0.5">
                  <span>Сторінка {d.page}</span>
                  <span className="font-mono">
                    очікувано {d.expected_cols}, знайдено {d.found_cols}
                  </span>
                </div>
              ))}
            </div>
          </div>
        )}

        {preview.preview_rows?.length > 0 && (
          <div>
            <p className="mb-1 text-sm font-medium text-ink">
              Перші {preview.preview_rows.length} рядків — перевір, що дані на місці:
            </p>
            <div className="overflow-x-auto rounded-lg border border-ink/10 bg-white">
              <table className="w-full min-w-max text-left text-xs">
                <thead>
                  <tr className="border-b border-ink/10 text-ink/50">
                    {preview.column_headers.map((h, i) => (
                      <th key={i} className="whitespace-nowrap px-2 py-1.5 font-medium">
                        {h}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {preview.preview_rows.map((row, i) => (
                    <tr key={i} className="border-b border-ink/5 last:border-0">
                      {preview.column_headers.map((h, j) => (
                        <td key={j} className="whitespace-nowrap px-2 py-1.5 text-ink/80">
                          {row[h]}
                        </td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        )}

        {preview.balance_check?.checked && (
          <p className="text-xs text-ink/50">
            Перевірка балансу: збіг у {Math.round(preview.balance_check.match_rate * 100)}% рядків
          </p>
        )}

        <p className="text-sm text-ink/70">Знайдено {preview.total_rows} транзакцій.</p>

        <div>
          <label className="mb-1 block text-sm font-medium text-ink">Банк</label>
          <select
            value={bankId}
            onChange={(e) => setBankId(e.target.value)}
            className="w-full rounded-lg border border-ink/15 bg-white px-3 py-2 text-sm outline-none focus:border-forest focus:ring-1 focus:ring-forest"
          >
            <option value="" disabled>
              Оберіть банк...
            </option>
            {banks.map((b) => (
              <option key={b.id} value={b.id}>
                {b.name}
              </option>
            ))}
            <option value={CUSTOM_BANK_VALUE}>✏️ Інший банк...</option>
          </select>
        </div>

        {isCustomBank && (
          <div className="space-y-2">
            <input
              type="text"
              required
              placeholder="Назва банку"
              value={customBankName}
              onChange={(e) => setCustomBankName(e.target.value)}
              className="w-full rounded-lg border border-ink/15 bg-white px-3 py-2 text-sm outline-none focus:border-forest focus:ring-1 focus:ring-forest"
            />
            <input
              type="text"
              placeholder="МФО (опційно)"
              value={customBankMfo}
              onChange={(e) => setCustomBankMfo(e.target.value)}
              className="w-full rounded-lg border border-ink/15 bg-white px-3 py-2 text-sm outline-none focus:border-forest focus:ring-1 focus:ring-forest"
            />
          </div>
        )}

        {error && <p className="rounded-lg bg-brick/10 px-3 py-2 text-sm text-brick">{error}</p>}

        <button
          onClick={handleConfirm}
          disabled={
            isSubmitting ||
            !hasBankSelected ||
            preview.warnings?.some((w) => w.severity === "blocking")
          }
          className="w-full rounded-lg bg-forest px-4 py-2.5 font-medium text-paper transition hover:bg-forest-light disabled:opacity-50"
        >
          {isSubmitting ? "Імпортуємо..." : "Підтвердити імпорт"}
        </button>
      </div>
    );
  }

  return (
    <form onSubmit={handlePreview} className="space-y-4">
      <div>
        <label className="mb-1 block text-sm font-medium text-ink">
          Виписка з банку (.csv, .xlsx, .xls, .pdf)
        </label>
        <input
          type="file"
          accept=".csv,.xlsx,.xls,.pdf"
          required
          onChange={(e) => setFile(e.target.files[0])}
          className="w-full rounded-lg border border-ink/15 bg-white px-3 py-2.5 text-sm outline-none file:mr-3 file:rounded-md file:border-0 file:bg-forest file:px-3 file:py-1.5 file:text-paper"
        />
        <p className="mt-1 text-xs text-ink/50">
          PDF-виписки зазвичай без колонки "Категорія" — категоризація відбудеться автоматично за описом.
        </p>
      </div>
      {error && <p className="rounded-lg bg-brick/10 px-3 py-2 text-sm text-brick">{error}</p>}
      <button
        type="submit"
        disabled={isSubmitting || !file}
        className="w-full rounded-lg bg-forest px-4 py-2.5 font-medium text-paper transition hover:bg-forest-light disabled:opacity-50"
      >
        {isSubmitting ? "Аналізуємо файл..." : "Завантажити й переглянути"}
      </button>
    </form>
  );
}

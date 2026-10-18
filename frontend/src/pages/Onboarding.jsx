import { useState } from "react";
import { useNavigate } from "react-router-dom";
import MonobankConnect from "../components/MonobankConnect";
import CsvImport from "../components/CsvImport";
import ManualEntry from "../components/ManualEntry";

const SOURCES = [
  {
    id: "monobank",
    title: "Підключити Monobank",
    description: "Автоматична синхронізація через API — найшвидший спосіб",
  },
  {
    id: "csv",
    title: "Завантажити виписку",
    description: "CSV, XLSX або PDF з будь-якого банку",
  },
  {
    id: "manual",
    title: "Ввести вручну",
    description: "Додавай транзакції по одній",
  },
];

export default function Onboarding() {
  const [source, setSource] = useState(null);
  const navigate = useNavigate();

  function handleDone() {
    navigate("/dashboard");
  }

  return (
    <div className="max-w-md">
      <h1 className="mb-1 font-display text-2xl font-semibold text-forest">
        Звідки взяти дані?
      </h1>
      <p className="mb-8 text-sm text-ink/60">
        Обери спосіб, яким зручно почати — інші джерела завжди можна додати пізніше
      </p>

      {!source && (
        <div className="space-y-3">
          {SOURCES.map((s) => (
            <button
              key={s.id}
              onClick={() => setSource(s.id)}
              className="w-full rounded-xl border border-ink/10 bg-white p-4 text-left transition hover:border-forest hover:shadow-sm"
            >
              <p className="font-medium text-ink">{s.title}</p>
              <p className="text-sm text-ink/50">{s.description}</p>
            </button>
          ))}
        </div>
      )}

      {source && (
        <div>
          <button
            onClick={() => setSource(null)}
            className="mb-4 text-sm text-ink/50 hover:text-forest"
          >
            ← Обрати інше джерело
          </button>

          <div className="rounded-xl border border-ink/10 bg-white p-5">
            {source === "monobank" && <MonobankConnect onDone={handleDone} />}
            {source === "csv" && <CsvImport onDone={handleDone} />}
            {source === "manual" && <ManualEntry onDone={handleDone} />}
          </div>
        </div>
      )}
    </div>
  );
}

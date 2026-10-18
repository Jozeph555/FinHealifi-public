# FinHealifi — фронтенд

React 18 + Vite + Tailwind CSS. Сторінки — у `src/pages/`, компоненти джерел даних (Monobank, імпорт виписки, ручне введення) — у `src/components/`. Усі запити йдуть через `src/services/api.js`: він додає JWT з `localStorage` і виходить із системи на відповідь 401.

```bash
cp .env.example .env   # VITE_API_URL — адреса бекенду, за замовчуванням http://localhost:8000
npm install
npm run dev            # http://localhost:5173
npm run build
```

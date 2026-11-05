// Рядок списку з сумою: назва й підпис | сума | дії.
// Сітка: назва займає решту ширини й переноситься, сума не розривається; дії на вузькому
// екрані — окремим рядком під назвою й сумою, з sm — третьою колонкою справа.
//
// title — назва; meta — рядок або елемент під назвою; amount — уже відформатована сума;
// amountClassName — колір і розмір суми; actions — кнопки (null — без колонки дій);
// className — оформлення рамки рядка.
export default function AmountRow({
  title,
  meta = null,
  amount,
  amountClassName = "text-lg text-ink",
  actions = null,
  className = "rounded-lg border border-ink/10 p-3",
}) {
  const columns = actions
    ? "grid-cols-[minmax(0,1fr)_auto] sm:grid-cols-[minmax(0,1fr)_auto_auto]"
    : "grid-cols-[minmax(0,1fr)_auto]";
  return (
    <div className={`grid items-center gap-x-3 gap-y-2 ${columns} ${className}`}>
      <div className="min-w-0">
        <div className="font-medium text-ink [overflow-wrap:anywhere]">{title}</div>
        {meta && <div className="text-xs text-ink/50">{meta}</div>}
      </div>
      <span className={`whitespace-nowrap font-mono ${amountClassName}`}>{amount}</span>
      {actions && <div className="col-span-2 flex flex-wrap justify-end gap-x-4 gap-y-1 sm:col-span-1">{actions}</div>}
    </div>
  );
}

// Кнопка дії рядка: дрібний текст без рамки; danger — червоне підсвічування при наведенні.
export function RowAction({ onClick, danger = false, disabled = false, children }) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      className={`whitespace-nowrap py-1 text-xs text-ink/40 disabled:opacity-50 ${
        danger ? "hover:text-brick" : "hover:text-forest"
      }`}
    >
      {children}
    </button>
  );
}

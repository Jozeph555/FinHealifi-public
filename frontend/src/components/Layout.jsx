import { useEffect, useState } from "react";
import { NavLink, useNavigate } from "react-router-dom";
import { useAuth } from "../context/AuthContext";
import api from "../services/api";
import { demoDateText } from "../utils/debtPlan";

const NAV_ITEMS = [
  { to: "/dashboard", label: "Dashboard" },
  { to: "/transactions", label: "Транзакції" },
  { to: "/debts", label: "Борги" },
  { to: "/assets", label: "Активи" },
  { to: "/loans", label: "Позики" },
  { to: "/advisor", label: "Радник" },
  { to: "/onboarding", label: "Додати дані" },
];

export default function Layout({ children }) {
  const { logout } = useAuth();
  const navigate = useNavigate();
  // Помітка, якщо сервер працює з демонстраційною датою (APP_TODAY_OVERRIDE).
  const [demoText, setDemoText] = useState(null);

  useEffect(() => {
    api
      .get("/debt/today")
      .then((res) => setDemoText(demoDateText(res.data)))
      .catch(() => setDemoText(null));
  }, []);

  function handleLogout() {
    logout();
    navigate("/login");
  }

  return (
    <div className="min-h-screen bg-paper">
      <header className="border-b border-ink/10 bg-white">
        {/* На вузькому екрані навігація — окремим рядком під назвою, з власною прокруткою. */}
        <div className="mx-auto flex max-w-4xl flex-wrap items-center justify-between gap-x-4 gap-y-2 px-4 py-3">
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-display text-lg font-semibold text-forest">
              FinHealifi
            </span>
            {demoText && (
              <span className="rounded-full bg-gold/20 px-2.5 py-0.5 text-xs font-medium text-ink/80">
                {demoText}
              </span>
            )}
          </div>

          <nav className="-mx-1 flex w-full items-center gap-1 overflow-x-auto px-1 sm:mx-0 sm:w-auto sm:px-0">
            {NAV_ITEMS.map((item) => (
              <NavLink
                key={item.to}
                to={item.to}
                className={({ isActive }) =>
                  `shrink-0 whitespace-nowrap rounded-md px-3 py-1.5 text-sm font-medium transition ${
                    isActive
                      ? "bg-forest/10 text-forest"
                      : "text-ink/60 hover:bg-ink/5 hover:text-ink"
                  }`
                }
              >
                {item.label}
              </NavLink>
            ))}
            <button
              onClick={handleLogout}
              className="ml-2 shrink-0 whitespace-nowrap rounded-md px-3 py-1.5 text-sm font-medium text-ink/50 transition hover:bg-brick/10 hover:text-brick"
            >
              Вийти
            </button>
          </nav>
        </div>
      </header>

      <main className="mx-auto max-w-4xl px-4 py-8">{children}</main>
    </div>
  );
}

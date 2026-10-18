import { NavLink, useNavigate } from "react-router-dom";
import { useAuth } from "../context/AuthContext";

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

  function handleLogout() {
    logout();
    navigate("/login");
  }

  return (
    <div className="min-h-screen bg-paper">
      <header className="border-b border-ink/10 bg-white">
        <div className="mx-auto flex max-w-4xl items-center justify-between px-4 py-3">
          <span className="font-display text-lg font-semibold text-forest">
            FinHealifi
          </span>

          <nav className="flex items-center gap-1">
            {NAV_ITEMS.map((item) => (
              <NavLink
                key={item.to}
                to={item.to}
                className={({ isActive }) =>
                  `rounded-md px-3 py-1.5 text-sm font-medium transition ${
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
              className="ml-2 rounded-md px-3 py-1.5 text-sm font-medium text-ink/50 transition hover:bg-brick/10 hover:text-brick"
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

import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useAuth } from "../context/AuthContext";

export default function Register() {
  const { register } = useAuth();
  const navigate = useNavigate();

  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [error, setError] = useState(null);
  const [isSubmitting, setIsSubmitting] = useState(false);

  async function handleSubmit(e) {
    e.preventDefault();
    setError(null);

    if (password.length < 8) {
      setError("Пароль має містити щонайменше 8 символів");
      return;
    }
    if (password !== confirmPassword) {
      setError("Паролі не збігаються");
      return;
    }

    setIsSubmitting(true);
    try {
      await register(email, password);
      // Новий користувач без даних — одразу на онбординг, не dashboard
      navigate("/onboarding");
    } catch (err) {
      setError(err.response?.data?.detail || "Не вдалося зареєструватись. Спробуй ще раз.");
    } finally {
      setIsSubmitting(false);
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-paper px-4">
      <div className="w-full max-w-sm">
        <h1 className="mb-1 font-display text-3xl font-semibold text-forest">
          FinHealifi
        </h1>
        <p className="mb-8 text-sm text-ink/60">
          Створи акаунт і почни діагностику фінансового здоров'я
        </p>

        <form onSubmit={handleSubmit} className="space-y-4">
          <div>
            <label htmlFor="email" className="mb-1 block text-sm font-medium text-ink">
              Email
            </label>
            <input
              id="email"
              type="email"
              required
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              className="w-full rounded-lg border border-ink/15 bg-white px-3 py-2.5 text-ink outline-none transition focus:border-forest focus:ring-1 focus:ring-forest"
              placeholder="tobi@example.com"
            />
          </div>

          <div>
            <label htmlFor="password" className="mb-1 block text-sm font-medium text-ink">
              Пароль
            </label>
            <input
              id="password"
              type="password"
              required
              minLength={8}
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              className="w-full rounded-lg border border-ink/15 bg-white px-3 py-2.5 text-ink outline-none transition focus:border-forest focus:ring-1 focus:ring-forest"
              placeholder="Мінімум 8 символів"
            />
          </div>

          <div>
            <label htmlFor="confirmPassword" className="mb-1 block text-sm font-medium text-ink">
              Повтори пароль
            </label>
            <input
              id="confirmPassword"
              type="password"
              required
              value={confirmPassword}
              onChange={(e) => setConfirmPassword(e.target.value)}
              className="w-full rounded-lg border border-ink/15 bg-white px-3 py-2.5 text-ink outline-none transition focus:border-forest focus:ring-1 focus:ring-forest"
              placeholder="••••••••"
            />
          </div>

          {error && (
            <p className="rounded-lg bg-brick/10 px-3 py-2 text-sm text-brick">{error}</p>
          )}

          <button
            type="submit"
            disabled={isSubmitting}
            className="w-full rounded-lg bg-forest px-4 py-2.5 font-medium text-paper transition hover:bg-forest-light disabled:opacity-50"
          >
            {isSubmitting ? "Створюємо акаунт..." : "Зареєструватись"}
          </button>
        </form>

        <p className="mt-6 text-center text-sm text-ink/60">
          Вже маєш акаунт?{" "}
          <Link to="/login" className="font-medium text-forest hover:underline">
            Увійти
          </Link>
        </p>
      </div>
    </div>
  );
}

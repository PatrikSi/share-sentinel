import { Link, useLocation, useNavigate } from "react-router-dom";

import { clearTokens, getRefreshToken } from "@/lib/auth";
import { ThemeToggle } from "@/components/theme-toggle";

export function TopNav() {
  const location = useLocation();
  const navigate = useNavigate();
  const API_BASE = (import.meta.env.VITE_API_BASE_URL as string) || "/api";

  async function logout() {
    const refreshToken = getRefreshToken();
    if (refreshToken) {
      try {
        await fetch(`${API_BASE}/auth/logout`, {
          method: "POST",
          credentials: "include",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ refresh_token: refreshToken }),
        });
      } catch {
        // Local cleanup still proceeds.
      }
    }
    clearTokens();
    navigate("/");
  }

  const navItems = [
    { to: "/projects", label: "Operations", match: "/projects" },
    { to: "/account", label: "Account", match: "/account" },
    { to: "/settings/users", label: "Settings", match: "/settings" },
  ];

  return (
    <header className="app-nav">
      <div className="app-nav-inner">
        <Link className="app-nav-brand" to="/projects">
          <span className="app-nav-title">share-sentinel</span>
        </Link>

        <nav className="app-nav-links">
          {navItems.map((item) => (
            <Link
              className={`app-nav-link ${location.pathname.startsWith(item.match) ? "is-active" : ""}`}
              key={item.to}
              to={item.to}
            >
              {item.label}
            </Link>
          ))}
        </nav>

        <div className="app-nav-actions">
          <ThemeToggle />
          <button className="app-logout-btn" onClick={logout}>
            Logout
          </button>
        </div>
      </div>
    </header>
  );
}

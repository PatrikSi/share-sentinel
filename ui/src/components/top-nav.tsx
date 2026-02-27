import { Link, useLocation, useNavigate } from "react-router-dom";

import { clearTokens } from "@/lib/auth";
import { ThemeToggle } from "@/components/theme-toggle";

export function TopNav() {
  const location = useLocation();
  const navigate = useNavigate();

  function logout() {
    clearTokens();
    navigate("/");
  }

  const navItems = [
    { to: "/projects", label: "Operations", match: "/projects" },
    { to: "/admin", label: "Administration", match: "/admin" },
  ];

  return (
    <header className="app-nav">
      <div className="app-nav-inner">
        <div className="app-nav-brand">
          <span className="app-nav-title">share-sentinel</span>
          <span className="app-nav-subtitle">Platform</span>
        </div>

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

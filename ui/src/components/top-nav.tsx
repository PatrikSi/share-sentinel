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
    <aside className="app-sidebar">
      <div className="app-sidebar-brand">
        <p className="app-sidebar-eyebrow">Platform</p>
        <h1 className="app-sidebar-title">share-sentinel</h1>
      </div>

      <nav className="app-sidebar-nav">
        {navItems.map((item) => (
          <Link
            className={`app-sidebar-link ${location.pathname.startsWith(item.match) ? "is-active" : ""}`}
            key={item.to}
            to={item.to}
          >
            {item.label}
          </Link>
        ))}
      </nav>

      <div className="app-sidebar-footer">
        <div className="mb-3">
          <p className="mb-2 text-[11px] font-semibold uppercase tracking-wider text-slate-500 dark:text-slate-400">
            Theme
          </p>
          <ThemeToggle />
        </div>
        <button className="app-logout-btn" onClick={logout}>
          Logout
        </button>
      </div>
    </aside>
  );
}

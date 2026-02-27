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

  return (
    <header className="sticky top-0 z-10 mb-6 border-b border-slate-200/70 bg-white/80 backdrop-blur dark:border-slate-800 dark:bg-slate-950/80">
      <div className="mx-auto flex max-w-7xl items-center justify-between px-4 py-3">
        <div className="flex items-center gap-4 text-sm font-semibold uppercase tracking-[0.2em] text-slate-700 dark:text-slate-200">
          <span>Share Sentinel</span>
          <Link className={location.pathname.startsWith("/projects") ? "text-ember" : ""} to="/projects">
            Projects
          </Link>
          <Link className={location.pathname.startsWith("/admin") ? "text-ember" : ""} to="/admin">
            Admin
          </Link>
        </div>
        <div className="flex items-center gap-3">
          <ThemeToggle />
          <button
            className="rounded-lg border border-slate-300 px-3 py-2 text-xs font-semibold uppercase tracking-widest hover:bg-slate-100 dark:border-slate-700 dark:hover:bg-slate-800"
            onClick={logout}
          >
            Logout
          </button>
        </div>
      </div>
    </header>
  );
}

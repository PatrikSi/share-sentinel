import { useEffect, useState } from "react";

import { apiFetch } from "@/lib/api";

const THEMES = ["system", "light", "dark"] as const;
type Theme = (typeof THEMES)[number];

function applyTheme(theme: Theme) {
  const root = document.documentElement;
  if (theme === "system") {
    const dark = window.matchMedia("(prefers-color-scheme: dark)").matches;
    root.classList.toggle("dark", dark);
    return;
  }
  root.classList.toggle("dark", theme === "dark");
}

export function ThemeToggle() {
  const [theme, setTheme] = useState<Theme>("system");

  useEffect(() => {
    const saved = (localStorage.getItem("share_sentinel_theme") as Theme | null) || "system";
    setTheme(saved);
    applyTheme(saved);
  }, []);

  async function updateTheme(next: Theme) {
    setTheme(next);
    localStorage.setItem("share_sentinel_theme", next);
    applyTheme(next);
    try {
      await apiFetch("/auth/me/theme", {
        method: "PATCH",
        body: JSON.stringify({ ui_theme: next }),
      });
    } catch {
      // Keep local preference even when backend update fails.
    }
  }

  return (
    <div className="flex items-center gap-2 rounded-full border border-slate-300 bg-white/70 p-1 text-xs dark:border-slate-700 dark:bg-slate-900/70">
      {THEMES.map((entry) => (
        <button
          key={entry}
          onClick={() => updateTheme(entry)}
          className={`rounded-full px-3 py-1 uppercase tracking-wide transition ${
            theme === entry
              ? "bg-ember text-white"
              : "text-slate-700 hover:bg-slate-200 dark:text-slate-200 dark:hover:bg-slate-700"
          }`}
        >
          {entry}
        </button>
      ))}
    </div>
  );
}

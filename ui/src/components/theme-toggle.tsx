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
    <label className="app-theme-select">
      <span className="sr-only">Color theme</span>
      <span aria-hidden="true">◐</span>
      <select onChange={(event) => updateTheme(event.target.value as Theme)} value={theme}>
        {THEMES.map((entry) => <option key={entry} value={entry}>{entry[0].toUpperCase() + entry.slice(1)}</option>)}
      </select>
    </label>
  );
}

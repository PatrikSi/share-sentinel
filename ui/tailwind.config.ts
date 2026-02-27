import type { Config } from "tailwindcss";

const config: Config = {
  darkMode: "class",
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      fontFamily: {
        sans: ["'Space Grotesk'", "sans-serif"],
        mono: ["'IBM Plex Mono'", "monospace"],
      },
      colors: {
        ink: "#0f172a",
        mist: "#e2e8f0",
        ember: "#ea580c",
        pine: "#14532d",
      },
      boxShadow: {
        panel: "0 12px 40px rgba(15, 23, 42, 0.15)",
      },
    },
  },
  plugins: [],
};

export default config;

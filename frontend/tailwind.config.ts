import type { Config } from "tailwindcss";

// Colours are CSS variables (src/design/tokens.css), so light and dark come
// from one set of class names.
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        bg: "var(--bg)",
        surface: "var(--surface)",
        "surface-2": "var(--surface-2)",
        line: "var(--border)",
        "line-strong": "var(--border-strong)",
        ink: "var(--text)",
        mute: "var(--text-muted)",
        accent: "var(--accent)",
        "accent-contrast": "var(--accent-contrast)",
        up: "var(--up)",
        down: "var(--down)",
        "up-soft": "var(--up-soft)",
        "down-soft": "var(--down-soft)",
        market: "var(--c-market)",
        sector: "var(--c-sector)",
        company: "var(--c-company)",
        "warn-soft": "var(--warn-soft)",
      },
      borderRadius: { control: "var(--radius-control)", card: "var(--radius-card)" },
      boxShadow: { pop: "var(--shadow-pop)" },
      fontSize: {
        display: ["32px", { lineHeight: "40px", letterSpacing: "-0.01em", fontWeight: "600" }],
        "display-sm": ["24px", { lineHeight: "32px", letterSpacing: "-0.01em", fontWeight: "600" }],
        title: ["24px", { lineHeight: "32px", fontWeight: "600" }],
        heading: ["18px", { lineHeight: "26px", fontWeight: "600" }],
        "body-lg": ["16px", { lineHeight: "26px" }],
        body: ["14px", { lineHeight: "20px" }],
        table: ["13px", { lineHeight: "20px" }],
        caption: ["12px", { lineHeight: "16px" }],
      },
      spacing: { 4.5: "18px" },
    },
  },
  plugins: [],
} satisfies Config;

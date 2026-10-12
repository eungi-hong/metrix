import type { Config } from "tailwindcss";

export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      boxShadow: {
        panel: "0 1px 2px rgba(0, 0, 0, 0.04), 0 8px 24px rgba(0, 0, 0, 0.035)",
      },
      colors: {
        ink: "#0a0a0a",
        mute: "#737373",
        canvas: "#f5f5f5",
        line: "#e5e5e5",
        panel: "#ffffff",
        surface: "#fafafa",
        "surface-raised": "#efefef",
        accent: "#0a0a0a",
        "accent-hover": "#262626",
        "accent-soft": "#f0f0f0",
        "accent-line": "#d4d4d4",
        positive: "#119a72",
        "positive-soft": "#e5f5ef",
        negative: "#c85658",
        "negative-soft": "#fbecec",
        warning: "#404040",
        "warning-soft": "#f5f5f5",
      },
    },
  },
  plugins: [],
} satisfies Config;

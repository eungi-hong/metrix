import type { Config } from "tailwindcss";

export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      boxShadow: {
        panel: "0 10px 30px rgba(29, 28, 53, 0.05)",
      },
      colors: {
        ink: "#172033",
        mute: "#667085",
        canvas: "#f4f6f8",
        line: "#e2e8f0",
        accent: "#2563eb",
      },
    },
  },
  plugins: [],
} satisfies Config;

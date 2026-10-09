import type { Config } from "tailwindcss";

export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      boxShadow: {
        panel: "0 10px 30px rgba(29, 28, 53, 0.05)",
      },
      colors: {
        ink: "#1d1d2c",
        mute: "#747389",
        canvas: "#f7f7fb",
        line: "#e8e8ef",
        accent: "#5b55d9",
      },
    },
  },
  plugins: [],
} satisfies Config;

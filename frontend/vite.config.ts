import { loadEnv } from "vite";
import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

export const API_BASE_URL_VAR = "VITE_METRIX_API_BASE_URL";

// The API URL is baked into the bundle at build time. Without it the app falls
// back to http://localhost:8000, which in a deployed build fails in every
// visitor's browser while the build itself succeeds. Dev and tests keep that default.
export function requireApiBaseUrl(mode: string, env: Record<string, string>) {
  if (mode === "production" && !env[API_BASE_URL_VAR]?.trim()) {
    throw new Error(`${API_BASE_URL_VAR} must be set for a production build (e.g. https://api.example.com). Without it the app would call http://localhost:8000.`);
  }
}

export default defineConfig(({ mode }) => {
  requireApiBaseUrl(mode, loadEnv(mode, ".", "VITE_"));
  return {
    plugins: [react()],
    test: {
      environment: "jsdom",
      setupFiles: ["./src/test/setup.ts"],
      css: true,
    },
  };
});

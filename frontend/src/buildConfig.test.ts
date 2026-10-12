// @vitest-environment node
// The build config imports Vite, whose esbuild refuses to load under jsdom.
import { describe, expect, it } from "vitest";
import { API_BASE_URL_VAR, requireApiBaseUrl } from "../vite.config";

describe("production build guard", () => {
  it("refuses a production build without an API URL", () => {
    expect(() => requireApiBaseUrl("production", {})).toThrow(API_BASE_URL_VAR);
    expect(() => requireApiBaseUrl("production", { [API_BASE_URL_VAR]: "  " })).toThrow(API_BASE_URL_VAR);
  });

  it("accepts a production build with one", () => {
    expect(() => requireApiBaseUrl("production", { [API_BASE_URL_VAR]: "https://api.example.com" })).not.toThrow();
  });

  it("keeps the localhost default for dev and tests", () => {
    expect(() => requireApiBaseUrl("development", {})).not.toThrow();
    expect(() => requireApiBaseUrl("test", {})).not.toThrow();
  });
});

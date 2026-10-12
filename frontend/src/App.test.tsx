import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("./components/PriceChart", () => ({
  PriceChart: ({ selectedMovement }: { selectedMovement: { id: number } | null }) => <div data-testid="chart-selection">{selectedMovement?.id ?? "none"}</div>,
}));

import App from "./App";

describe("movement selection", () => {
  beforeEach(() => sessionStorage.clear());
  afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

  it("synchronizes a list selection to the chart and opens its evidence", () => {
    render(<App />);
    fireEvent.click(screen.getByRole("button", { name: "View demo data" }));
    fireEvent.click(screen.getByRole("tab", { name: "Movements" }));
    fireEvent.click(screen.getByRole("button", { name: /Jul 27, 2026/i }));
    expect(screen.getByTestId("chart-selection")).toHaveTextContent("72");
    expect(screen.getByRole("tab", { name: "News" })).toHaveAttribute("aria-selected", "true");
  });

  it("keeps Ask Metrix available without selecting a ticker on Home", () => {
    const view = render(<App />);
    const queries = within(view.container);
    fireEvent.click(queries.getByRole("button", { name: "Ask Metrix" }));
    const input = queries.getByRole("textbox", { name: "Ask Metrix" });
    expect(input).toBeEnabled();
    fireEvent.change(input, { target: { value: "What market themes matter this week?" } });
    expect(queries.getByRole("button", { name: "Send question" })).toBeEnabled();
  });

  it("answers on demo access without any setup", async () => {
    const answer = { conversation_id: "c1", ticker: null, answer: "Rates and AI capex.", sources: { movements: [], articles: [] }, grounded: false };
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify(answer), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    const view = render(<App />);
    const queries = within(view.container);
    fireEvent.click(queries.getByRole("button", { name: "Ask Metrix" }));
    fireEvent.change(queries.getByRole("textbox", { name: "Ask Metrix" }), { target: { value: "What market themes matter this week?" } });
    fireEvent.click(queries.getByRole("button", { name: "Send question" }));
    expect(await queries.findByText("Rates and AI capex.")).toBeInTheDocument();
    expect(fetchMock.mock.calls[0][1].headers).not.toHaveProperty("X-OpenAI-Api-Key");
  });

  it("explains why Ask Metrix can't answer after the connection is cleared", () => {
    const view = render(<App />);
    const queries = within(view.container);
    fireEvent.click(queries.getByRole("button", { name: "Provider key settings" }));
    fireEvent.click(screen.getByRole("button", { name: "Clear connection" }));
    fireEvent.click(queries.getByRole("button", { name: "Ask Metrix" }));
    fireEvent.change(queries.getByRole("textbox", { name: "Ask Metrix" }), { target: { value: "What drove NVDA's latest move?" } });
    fireEvent.click(queries.getByRole("button", { name: "Send question" }));
    expect(queries.getByRole("alert")).toHaveTextContent("Choose demo access or add both your Exa and OpenAI API keys");
  });

  it("resends the failed question when Retry follows a rate limit", async () => {
    const answer = { conversation_id: "c1", ticker: null, answer: "Rates and AI capex.", sources: { movements: [], articles: [] }, grounded: false };
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ error: "rate_limited", detail: "Chat limit reached." }), { status: 429, headers: { "Retry-After": "30" } }))
      .mockResolvedValueOnce(new Response(JSON.stringify(answer), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    const view = render(<App />);
    const queries = within(view.container);
    fireEvent.click(queries.getByRole("button", { name: "Ask Metrix" }));
    fireEvent.change(queries.getByRole("textbox", { name: "Ask Metrix" }), { target: { value: "What market themes matter this week?" } });
    fireEvent.click(queries.getByRole("button", { name: "Send question" }));
    fireEvent.click(await queries.findByRole("button", { name: "Retry" }));
    expect(await queries.findByText("Rates and AI capex.")).toBeInTheDocument();
    await waitFor(() => expect(queries.queryByRole("alert")).not.toBeInTheDocument());
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(queries.getAllByText("What market themes matter this week?")).toHaveLength(1);
  });
});

import { fireEvent, render, screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("./components/PriceChart", () => ({
  PriceChart: ({ selectedMovement }: { selectedMovement: { id: number } | null }) => <div data-testid="chart-selection">{selectedMovement?.id ?? "none"}</div>,
}));

import App from "./App";

describe("movement selection", () => {
  beforeEach(() => sessionStorage.clear());

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
});

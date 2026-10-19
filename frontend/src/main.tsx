import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StrictMode, lazy } from "react";
import { createRoot } from "react-dom/client";
import { RouterProvider, createBrowserRouter } from "react-router-dom";
import { SettingsProvider } from "./app/settings";
import { Shell } from "./app/Shell";
import { RouteError } from "./app/RouteError";
import "./index.css";

// Each screen is its own chunk, loaded when it is first visited.
const HomePage = lazy(() => import("./features/home/HomePage"));
const TickerPage = lazy(() => import("./features/ticker/TickerPage"));
const CasePage = lazy(() => import("./features/movement/CasePage"));
const AskPage = lazy(() => import("./features/chat/AskPage"));
const PulsePage = lazy(() => import("./features/pulse/PulsePage"));
const WatchlistPage = lazy(() => import("./features/watchlist/WatchlistPage"));
const MethodologyPage = lazy(() => import("./features/methodology/MethodologyPage"));
const NotFound = lazy(() => import("./app/NotFound"));

export const routes = [
  {
    element: <Shell />,
    errorElement: <RouteError />,
    children: [
      { path: "/", element: <HomePage /> },
      { path: "/t/:symbol", element: <TickerPage /> },
      { path: "/t/:symbol/m/:date", element: <CasePage /> },
      { path: "/ask", element: <AskPage /> },
      { path: "/pulse", element: <PulsePage /> },
      { path: "/watchlist", element: <WatchlistPage /> },
      { path: "/methodology", element: <MethodologyPage /> },
      { path: "*", element: <NotFound /> },
    ],
  },
];

const queryClient = new QueryClient({ defaultOptions: { queries: { refetchOnWindowFocus: false } } });

async function start() {
  // Mocked API for end-to-end tests and screenshots (VITE_MSW=1 only).
  if (import.meta.env.VITE_MSW === "1") {
    const { worker } = await import("./mocks/browser");
    await worker.start({ onUnhandledRequest: "bypass", quiet: true });
  }
  createRoot(document.getElementById("root")!).render(
    <StrictMode>
      <SettingsProvider>
        <QueryClientProvider client={queryClient}>
          <RouterProvider router={createBrowserRouter(routes)} />
        </QueryClientProvider>
      </SettingsProvider>
    </StrictMode>,
  );
}

void start();

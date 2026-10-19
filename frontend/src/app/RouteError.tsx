import { Link, useRouteError } from "react-router-dom";

export function RouteError() {
  const error = useRouteError() as Error | undefined;
  return (
    <main className="mx-auto max-w-xl px-4 py-16">
      <h1 className="text-title text-ink">This page failed to load</h1>
      <p className="mt-2 text-body text-mute">{error?.message?.includes("dynamically imported") ? "A newer version of Metrix was released. Reload to get it." : "Something in this page broke. Reload, or go back to the home page."}</p>
      <p className="mt-4 flex gap-4 text-body"><button className="underline" onClick={() => window.location.reload()}>Reload</button><Link className="underline" to="/">Home</Link></p>
    </main>
  );
}

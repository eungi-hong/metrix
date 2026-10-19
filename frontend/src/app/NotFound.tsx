import { Link } from "react-router-dom";

export default function NotFound() {
  return (
    <div className="py-12">
      <h1 className="text-title text-ink">Nothing here</h1>
      <p className="mt-2 text-body text-mute">Search for a ticker with <kbd className="mono">/</kbd>, or start from the <Link className="underline" to="/">home page</Link>.</p>
    </div>
  );
}

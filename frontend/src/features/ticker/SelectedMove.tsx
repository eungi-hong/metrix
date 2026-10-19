import { Link } from "react-router-dom";
import type { Movement } from "../../api/types";
import { Change, DriverChip, Empty, Kbd, buttonClass } from "../../design/ui";
import { longDate, signedNumber, timingLabel } from "../../lib/format";
import { DecompositionBar } from "../movement/Decomposition";
import { EvidenceLine } from "./EvidenceLine";

export function SelectedMove({ symbol, movement }: { symbol: string; movement: Movement | null }) {
  if (!movement) return <Empty title="Select a move">Pick a row in the table or a marker on the chart.</Empty>;
  const top = movement.news.slice(0, 3);
  return (
    <article aria-labelledby="selected-move">
      <h2 id="selected-move" className="flex flex-wrap items-baseline gap-x-3 text-heading text-ink">
        {longDate(movement.date)} <Change value={movement.daily_return} className="text-heading" />
      </h2>
      <p className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-caption text-mute">
        {movement.decomposition?.sar != null && <span className="num">SAR {signedNumber(movement.decomposition.sar)}</span>}
        <DriverChip driver={movement.decomposition?.dominant} />
      </p>
      <div className="mt-3"><DecompositionBar decomposition={movement.decomposition} total={movement.daily_return} warning={movement.decomposition_warning} compact /></div>
      <ol className="mt-4 grid gap-2">
        {top.map((link, i) => (
          <li key={link.article.id} className="grid grid-cols-[16px_1fr] gap-2 text-body">
            <span className="num text-mute">{i + 1}</span>
            <span>
              <span className="text-ink">{link.article.title ?? link.article.url}</span>
              <span className="block text-caption text-mute">{link.article.source}{link.timing ? `, ${timingLabel[link.timing].toLowerCase()}` : ""}</span>
            </span>
          </li>
        ))}
      </ol>
      {!top.length && <div className="mt-3"><EvidenceLine status={movement.evidence} /></div>}
      <p className="mt-3 text-caption text-mute">{movement.news.length} {movement.news.length === 1 ? "article" : "articles"}, {movement.filings.length} {movement.filings.length === 1 ? "filing" : "filings"}</p>
      <Link to={`/t/${symbol}/m/${movement.date}`} className={buttonClass("primary", "mt-4 w-full")}>
        Open case file <span className="hidden md:inline"><Kbd>Enter</Kbd></span>
      </Link>
    </article>
  );
}

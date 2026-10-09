import { FlaskConical } from "lucide-react";

export function DemoDataBanner({ onUseDemo }: { onUseDemo: () => void }) {
  return <div className="mx-5 mt-4 rounded-xl border border-blue-100 bg-blue-50 p-3 text-xs leading-5 text-blue-950"><div className="flex gap-2"><FlaskConical size={15} className="mt-0.5 shrink-0 text-accent" /><p><span className="font-semibold">Demo data.</span> Local fixture data only.</p></div><button onClick={onUseDemo} className="mt-2 font-semibold text-accent hover:underline">View demo data</button></div>;
}

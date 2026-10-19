import { useEffect, useState } from "react";

/** The chart's height: reserved before data arrives, so nothing shifts. 360 / 300 / 240 px. */
export function chartHeight(width: number): number {
  return width >= 1024 ? 360 : width >= 640 ? 300 : 240;
}

export function useMediaHeight(): number {
  const [height, setHeight] = useState(() => chartHeight(typeof window === "undefined" ? 1440 : window.innerWidth));
  useEffect(() => {
    const onResize = () => setHeight(chartHeight(window.innerWidth));
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);
  return height;
}

// Time helpers. Formatting is plain arithmetic (no Intl, no time zone), so the
// server and the browser always render the same string.

const ISO = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$/;

/**
 * Shift every timestamp in a snapshot so its anchor reads as `now`. Preview
 * data then always looks recent ("4h ago"), however old the file is.
 */
export function rebase<T>(value: T, anchor: string, now: Date): T {
  const offset = now.getTime() - Date.parse(anchor);
  const walk = (v: unknown): unknown => {
    if (typeof v === "string") return ISO.test(v) ? new Date(Date.parse(v) + offset).toISOString() : v;
    if (Array.isArray(v)) return v.map(walk);
    if (v && typeof v === "object") return Object.fromEntries(Object.entries(v).map(([k, x]) => [k, walk(x)]));
    return v;
  };
  return walk(value) as T;
}

export const HOUR = 3_600_000;
export const DAY = 24 * HOUR;

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** "just now", "5m ago", "3h ago", "2d ago", then "Sep 30". */
export function ago(iso: string, now: number): string {
  const ms = now - Date.parse(iso);
  if (ms < 60_000) return "just now";
  if (ms < HOUR) return `${Math.floor(ms / 60_000)}m ago`;
  if (ms < DAY) return `${Math.floor(ms / HOUR)}h ago`;
  if (ms < 8 * DAY) return `${Math.floor(ms / DAY)}d ago`;
  const d = new Date(iso);
  return `${MONTHS[d.getUTCMonth()]} ${d.getUTCDate()}`;
}

/** A duration in words: "22 hours", "4 days". */
export function duration(ms: number): string {
  if (ms < HOUR) return `${Math.max(1, Math.round(ms / 60_000))} minutes`;
  if (ms < DAY) {
    const h = Math.floor(ms / HOUR);
    return h === 1 ? "1 hour" : `${h} hours`;
  }
  const days = Math.floor(ms / DAY);
  return days === 1 ? "1 day" : `${days} days`;
}

/** "Oct 6, 14:05 UTC", for tooltips. */
export function stamp(iso: string): string {
  const d = new Date(iso);
  const hh = String(d.getUTCHours()).padStart(2, "0");
  const mm = String(d.getUTCMinutes()).padStart(2, "0");
  return `${MONTHS[d.getUTCMonth()]} ${d.getUTCDate()}, ${hh}:${mm} UTC`;
}

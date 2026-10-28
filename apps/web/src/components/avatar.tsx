// Initials on a color picked from the name, so a person keeps one color
// everywhere (dashboard, graph, settings).

// Theme tokens (--avatar-1..8 in globals.css), so each tone is tuned for light and dark.
const PALETTE = Array.from({ length: 8 }, (_, i) => `var(--avatar-${i + 1})`);

export function initials(name: string): string {
  const parts = name.trim().split(/\s+/).filter(Boolean);
  if (parts.length === 0) return "?";
  const first = parts[0][0];
  const last = parts.length > 1 ? parts[parts.length - 1][0] : "";
  return (first + last).toUpperCase();
}

export function colorFor(name: string): string {
  let hash = 0;
  for (const ch of name) hash = (hash * 31 + ch.charCodeAt(0)) >>> 0;
  return PALETTE[hash % PALETTE.length];
}

export function Avatar({ name, size = "md", title }: { name: string; size?: "sm" | "md" | "lg"; title?: string }) {
  const cls = size === "md" ? "avatar" : `avatar avatar-${size}`;
  return (
    <span className={cls} style={{ ["--avatar" as string]: colorFor(name) }} title={title ?? name} aria-hidden={title ? undefined : true}>
      {initials(name)}
    </span>
  );
}

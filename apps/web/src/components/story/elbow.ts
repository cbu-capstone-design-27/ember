// The connector path shared by <Links> (links.tsx) and the landing's
// architecture figure. Plain module, so server components can call it.

const RADIUS = 10;

/** An orthogonal path from (x1, y1) to (x2, y2) that turns at the midpoint
 * with rounded corners. axis "y" leaves vertically, axis "x" horizontally. */
export function elbow(x1: number, y1: number, x2: number, y2: number, axis: "x" | "y" = "y", radius = RADIUS) {
  if (axis === "y") {
    const dx = x2 - x1;
    if (Math.abs(dx) < 0.5) return `M${x1} ${y1}V${y2}`;
    const s = Math.sign(dx);
    const ym = (y1 + y2) / 2;
    const r = Math.min(radius, Math.abs(dx) / 2, Math.abs(y2 - y1) / 2);
    return `M${x1} ${y1}V${ym - r}A${r} ${r} 0 0 ${s > 0 ? 0 : 1} ${x1 + s * r} ${ym}H${x2 - s * r}A${r} ${r} 0 0 ${s > 0 ? 1 : 0} ${x2} ${ym + r}V${y2}`;
  }
  const dy = y2 - y1;
  if (Math.abs(dy) < 0.5) return `M${x1} ${y1}H${x2}`;
  const s = Math.sign(dy);
  const xm = (x1 + x2) / 2;
  const r = Math.min(radius, Math.abs(dy) / 2, Math.abs(x2 - x1) / 2);
  return `M${x1} ${y1}H${xm - r}A${r} ${r} 0 0 ${s > 0 ? 1 : 0} ${xm} ${y1 + s * r}V${y2 - s * r}A${r} ${r} 0 0 ${s > 0 ? 0 : 1} ${xm + r} ${y2}H${x2}`;
}

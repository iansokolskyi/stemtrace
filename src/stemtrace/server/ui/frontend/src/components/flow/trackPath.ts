/**
 * One track as an SVG path: it runs level out of (x1, y1), eases to y2 through a round S-curve
 * centred on the midpoint, and arrives level at (x2, y2). Siblings share x1 and x2, so their
 * curves start and end at the same x and read as one junction. The two handles are unequal
 * (0.56 and 0.4) so the S leans like a drawn stem. `fromJunction` starts the stroke where the
 * curve begins, so a failed branch lights its own section and not the trunk it shares with its
 * healthy siblings. Ported from the prototype's `trackD`.
 */
export function trackPath(
  x1: number,
  y1: number,
  x2: number,
  y2: number,
  fromJunction = false,
): string {
  const dy = y2 - y1
  if (Math.abs(dy) < 1) return `M${x1},${y1} H${x2}`
  const dir = x2 >= x1 ? 1 : -1
  const xm = (x1 + x2) / 2
  const sw = Math.min(26, Math.abs(x2 - x1) * 0.42)
  const xa = xm - dir * sw
  const xb = xm + dir * sw
  const w = 2 * sw
  const start = fromJunction ? `M${xa},${y1}` : `M${x1},${y1} H${xa}`
  const c1 = (xa + dir * w * 0.56).toFixed(1)
  const c2 = (xb - dir * w * 0.4).toFixed(1)
  return `${start} C${c1},${y1} ${c2},${y2} ${xb},${y2} H${x2}`
}

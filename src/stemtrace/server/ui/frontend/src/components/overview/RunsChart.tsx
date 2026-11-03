import {
  type KeyboardEvent,
  type PointerEvent,
  type ReactElement,
  useId,
  useLayoutEffect,
  useRef,
  useState,
} from 'react'
import { openerKey, openerProps, usePanel } from '@/components/triage/PanelContext'
import {
  clock,
  decodeScope,
  encodeScope,
  type RunsScope,
  spanLabel,
} from '@/components/triage/runsScope'
import { formatCount, formatPercent } from '@/utils/format'
import type { Bucket } from './model'

/** A brushed span of the chart, as ISO times. */
export interface Brush {
  from: string
  to: string
}

const MARGIN = { l: 40, r: 6, t: 22, b: 22 }
const GAP_MIN_BAR = 6
const DRAG_PX = 4

/** The tick step of the y axis: 1, 2 or 5 times a power of ten. */
function niceStep(raw: number): number {
  const power = 10 ** Math.floor(Math.log10(raw))
  const f = raw / power
  return (f <= 1 ? 1 : f <= 2 ? 2 : f <= 5 ? 5 : 10) * power
}

function useWidth(): [React.RefObject<HTMLDivElement | null>, number] {
  const ref = useRef<HTMLDivElement>(null)
  const [width, setWidth] = useState(0)
  useLayoutEffect(() => {
    const box = ref.current
    if (!box) return
    setWidth(box.clientWidth)
    const observer = new ResizeObserver(() => setWidth(box.clientWidth))
    observer.observe(box)
    return () => observer.disconnect()
  }, [])
  return [ref, width]
}

const barScope = (b: Bucket): RunsScope => ({
  kind: 'bar',
  from: b.start,
  to: b.end,
})
const brushScope = (brush: Brush): RunsScope => ({
  kind: 'brush',
  from: brush.from,
  to: brush.to,
})

interface RunsChartProps {
  buckets: Bucket[]
  /** The bucket size in seconds, for the title ("Runs per 5 min"). */
  bucketSeconds: number | undefined
  brush: Brush | null
  onBrush: (brush: Brush | null) => void
}

function bucketWord(seconds: number | undefined): string {
  if (!seconds) return ''
  if (seconds % 86_400 === 0) return `${seconds / 86_400} d`
  if (seconds % 3600 === 0) return `${seconds / 3600} h`
  if (seconds % 60 === 0) return `${seconds / 60} min`
  return `${seconds} s`
}

/**
 * Stacked success, failure and retry runs per bucket (the prototype's chart). Click a bar to open
 * its runs, drag across bars to brush a span ("Open runs" lists it), arrow keys and Enter do the
 * same from the keyboard, and Table lists the same numbers. Retry is hatched at 45 degrees.
 */
export function RunsChart({
  buckets,
  bucketSeconds,
  brush,
  onBrush,
}: RunsChartProps): ReactElement {
  const panel = usePanel()
  const [view, setView] = useState<'chart' | 'table'>('chart')
  const hatchId = `hatch-${useId().replace(/:/g, '')}`
  const [boxRef, boxWidth] = useWidth()
  const [hover, setHover] = useState<number | null>(null)
  const [kbIndex, setKbIndex] = useState<number | null>(null)
  const drag = useRef<{ x: number; index: number } | null>(null)
  const [dragSpan, setDragSpan] = useState<[number, number] | null>(null)

  const n = buckets.length
  const width = Math.max(300, boxWidth - 24)
  const height = width < 560 ? 180 : 240
  const iw = width - MARGIN.l - MARGIN.r
  const ih = height - MARGIN.t - MARGIN.b
  const bw = n ? iw / n : iw
  const total = (b: Bucket) => b.success + b.failure + b.retry
  const max = Math.max(1, ...buckets.map(total))
  const step = niceStep(max / 4)
  const top = Math.ceil(max / step) * step
  const y = (v: number) => MARGIN.t + ih - (v / top) * ih

  const indexAt = (event: PointerEvent<SVGSVGElement>): number => {
    const rect = event.currentTarget.getBoundingClientRect()
    const x = (event.clientX - rect.left) * (width / (rect.width || width)) - MARGIN.l
    return Math.max(0, Math.min(n - 1, Math.floor(x / bw)))
  }

  const onUp = (event: PointerEvent<SVGSVGElement>) => {
    const start = drag.current
    drag.current = null
    setDragSpan(null)
    if (!start || n === 0) return
    const index = indexAt(event)
    if (Math.abs(event.clientX - start.x) <= DRAG_PX) {
      panel.open({ kind: 'runs', id: encodeScope(barScope(buckets[index])) })
      return
    }
    const [a, z] = [Math.min(index, start.index), Math.max(index, start.index)]
    onBrush({ from: buckets[a].start, to: buckets[z].end })
  }

  const onKey = (event: KeyboardEvent<SVGSVGElement>) => {
    if (n === 0) return
    const current = kbIndex ?? n - 1
    if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') {
      event.preventDefault()
      const next = Math.max(0, Math.min(n - 1, current + (event.key === 'ArrowLeft' ? -1 : 1)))
      setKbIndex(next)
      setHover(next)
    } else if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault()
      panel.open({ kind: 'runs', id: encodeScope(barScope(buckets[current])) })
    }
  }

  // The bar or brush the open panel was opened from stays outlined
  const active = panel.activeKey
  const activeScope = panel.stack[0]?.kind === 'runs' ? decodeScope(panel.stack[0].id) : undefined
  const selected =
    activeScope?.kind === 'bar' ? buckets.findIndex((b) => b.start === activeScope.from) : -1
  const brushOpen =
    brush !== null && active === openerKey({ kind: 'runs', id: encodeScope(brushScope(brush)) })

  const brushIndexes = (() => {
    if (dragSpan) return dragSpan
    if (!brush || n === 0) return null
    const a = buckets.findIndex((b) => b.end > brush.from)
    const zIndex = buckets.map((b) => b.start < brush.to).lastIndexOf(true)
    return a < 0 || zIndex < a ? null : ([a, zIndex] as [number, number])
  })()

  const peaks = (() => {
    const found: number[] = []
    const minSep = Math.ceil(64 / bw)
    buckets
      .map((b, i) => ({ i, f: b.failure }))
      .filter((o) => o.f > 0)
      .sort((p, q) => q.f - p.f)
      .forEach((o) => {
        if (found.length < 3 && found.every((i) => Math.abs(i - o.i) >= minSep)) found.push(o.i)
      })
    return found
  })()

  const every = n > 24 ? 12 : 2
  const gap = bw > GAP_MIN_BAR ? 1.5 : 0.5
  const shown = hover !== null ? buckets[hover] : undefined
  // The tooltip sits right of the bar (the box has 12px of padding), and flips left near the edge
  const barCentre = MARGIN.l + (hover ?? 0) * bw + bw / 2 + 12
  const tipLeft = Math.max(4, barCentre + 190 > boxWidth ? barCentre - 190 : barCentre + 14)

  const title = bucketWord(bucketSeconds)
  const brushLabel = brush ? spanLabel(brush.from, brush.to) : ''

  return (
    <section aria-labelledby="h-chart" className="mb-4 min-w-0 rounded-lg bg-surface">
      <div className="flex flex-wrap items-center gap-x-3.5 gap-y-2.5 px-4 pt-3.5 pb-2">
        <h2 id="h-chart" className="text-sm font-bold">
          {title ? `Runs per ${title}` : 'Runs'}
        </h2>
        <div className="flex gap-3.5 text-xs text-muted">
          <Legend swatch="bg-success" label="Success" />
          <Legend swatch="bg-failure" label="Failure" />
          <Legend swatch="retry-hatch" label="Retry" />
        </div>
        <span className="grow" />
        {brush ? (
          <>
            <span className="inline-flex h-[26px] items-center gap-1.5 rounded-[3px] bg-raised pr-1 pl-2.5 text-xs">
              <span className="text-muted">Filtered to</span>
              <b data-testid="brush-label">{brushLabel}</b>
              <button
                type="button"
                aria-label="Clear time filter"
                onClick={() => onBrush(null)}
                className="h-[18px] rounded-[2px] px-2 text-xs text-muted hover:bg-hover hover:text-ink"
              >
                clear
              </button>
            </span>
            <button
              type="button"
              {...openerProps(panel, { kind: 'runs', id: encodeScope(brushScope(brush)) })}
              className="inline-flex h-[30px] items-center rounded-[3px] bg-raised px-3 text-xs font-semibold hover:bg-press data-[on]:bg-ink data-[on]:text-on-ink"
            >
              Open runs
            </button>
          </>
        ) : (
          <span className="text-xs text-muted">
            UTC.{' '}
            {view === 'table'
              ? 'Click a row to open its runs.'
              : 'Drag to select, click a bar to open its runs.'}
          </span>
        )}
        <fieldset
          aria-label="Chart view"
          className="m-0 inline-flex min-w-0 flex-none gap-0.5 rounded-[3px] border-0 bg-raised p-0.5"
        >
          {(['chart', 'table'] as const).map((mode) => (
            <button
              key={mode}
              type="button"
              aria-pressed={view === mode}
              onClick={() => setView(mode)}
              className="h-[26px] rounded-[2px] px-[11px] text-xs font-semibold text-muted hover:bg-hover hover:text-ink aria-pressed:bg-ink aria-pressed:text-on-ink aria-pressed:hover:bg-ink aria-pressed:hover:text-on-ink"
            >
              {mode === 'chart' ? 'Chart' : 'Table'}
            </button>
          ))}
        </fieldset>
      </div>

      <div ref={boxRef} className="relative px-3 pt-1.5 pb-3" data-testid="runs-chart">
        {view === 'table' ? (
          <ChartTable buckets={buckets} />
        ) : n === 0 ? (
          <p className="m-0 px-4 py-10 text-center text-xs text-muted">No runs in this window.</p>
        ) : (
          <>
            {/* biome-ignore lint/a11y/useSemanticElements: an svg cannot be a fieldset; it is one group the arrow keys move through */}
            <svg
              viewBox={`0 0 ${width} ${height}`}
              width={width}
              height={height}
              role="group"
              // biome-ignore lint/a11y/noNoninteractiveTabindex: the chart is keyboard-operable (arrows, Enter)
              tabIndex={0}
              aria-label="Stacked bars of successful, failed and retried runs per bucket. Arrow keys move between buckets, Enter opens the runs in a bucket."
              className="block h-auto w-full cursor-crosshair touch-pan-y select-none"
              onPointerDown={(e) => {
                if (e.button > 0) return
                drag.current = { x: e.clientX, index: indexAt(e) }
                try {
                  e.currentTarget.setPointerCapture(e.pointerId)
                } catch {
                  // Capture is unsupported here: the drag still works inside the svg
                }
              }}
              onPointerMove={(e) => {
                const index = indexAt(e)
                setHover(index)
                const start = drag.current
                if (start && Math.abs(e.clientX - start.x) > DRAG_PX) {
                  setDragSpan([Math.min(index, start.index), Math.max(index, start.index)])
                }
              }}
              onPointerUp={onUp}
              onPointerCancel={() => {
                drag.current = null
                setDragSpan(null)
              }}
              onPointerLeave={() => {
                if (!drag.current) setHover(null)
              }}
              onKeyDown={onKey}
              onBlur={() => setKbIndex(null)}
            >
              <defs>
                <pattern
                  id={hatchId}
                  width="6"
                  height="6"
                  patternUnits="userSpaceOnUse"
                  patternTransform="rotate(45)"
                >
                  <rect width="6" height="6" fill="var(--retry)" />
                  <rect width="2.5" height="6" fill="var(--surface)" />
                </pattern>
              </defs>
              {Array.from({ length: Math.floor(top / step) + 1 }, (_, k) => k * step).map((v) => (
                <g key={v}>
                  <line
                    x1={MARGIN.l}
                    x2={width - MARGIN.r}
                    y1={y(v)}
                    y2={y(v)}
                    stroke="var(--rule)"
                  />
                  <text
                    x={MARGIN.l - 6}
                    y={y(v) + 3.5}
                    textAnchor="end"
                    fontSize={11}
                    fill="var(--muted)"
                  >
                    {v}
                  </text>
                </g>
              ))}
              {buckets.map((b, i) => {
                const x = MARGIN.l + i * bw + gap / 2
                const w = Math.max(1, bw - gap)
                let acc = 0
                let drawn = false
                return (
                  <g
                    key={b.start}
                    data-testid="chart-bar"
                    data-start={b.start}
                    data-failure={b.failure}
                  >
                    {(
                      [
                        [b.success, 'var(--success)'],
                        [b.failure, 'var(--failure)'],
                        [b.retry, `url(#${hatchId})`],
                      ] as const
                    ).map(([count, fill]) => {
                      if (!count) return null
                      const y0 = y(acc)
                      const y1 = y(acc + count)
                      acc += count
                      const h = Math.max(0.5, y0 - y1 - (drawn ? 2 : 0))
                      drawn = true
                      return <rect key={fill} x={x} y={y1} width={w} height={h} fill={fill} />
                    })}
                  </g>
                )
              })}
              {peaks.map((i) => {
                const px = Math.max(
                  MARGIN.l + 24,
                  Math.min(width - MARGIN.r - 24, MARGIN.l + i * bw + bw / 2),
                )
                return (
                  <text
                    key={i}
                    x={px}
                    y={y(total(buckets[i])) - 6}
                    textAnchor="middle"
                    fontSize={11.5}
                    fontWeight={700}
                    fill="var(--ink)"
                    pointerEvents="none"
                    style={{ paintOrder: 'stroke', stroke: 'var(--surface)', strokeWidth: 3 }}
                  >
                    {buckets[i].failure} failed
                  </text>
                )
              })}
              {buckets.map((b, i) =>
                i % every === 0 ? (
                  <g key={b.start}>
                    <line
                      x1={MARGIN.l + i * bw}
                      x2={MARGIN.l + i * bw}
                      y1={MARGIN.t + ih}
                      y2={MARGIN.t + ih + 4}
                      stroke="var(--rule)"
                    />
                    <text
                      x={MARGIN.l + i * bw}
                      y={height - 6}
                      textAnchor="middle"
                      fontSize={11}
                      fill="var(--muted)"
                    >
                      {clock(b.start)}
                    </text>
                  </g>
                ) : null,
              )}
              {hover !== null && (
                <rect
                  x={MARGIN.l + hover * bw}
                  y={MARGIN.t}
                  width={bw}
                  height={ih}
                  fill="var(--ink)"
                  opacity={0.08}
                  pointerEvents="none"
                />
              )}
              {selected >= 0 && (
                <rect
                  data-testid="bar-selected"
                  x={MARGIN.l + selected * bw}
                  y={MARGIN.t}
                  width={bw}
                  height={ih}
                  fill="none"
                  stroke="var(--ink)"
                  strokeWidth={2}
                  pointerEvents="none"
                />
              )}
              {brushIndexes && (
                <rect
                  data-testid="brush"
                  data-on={brushOpen ? '' : undefined}
                  x={MARGIN.l + brushIndexes[0] * bw}
                  y={MARGIN.t}
                  width={(brushIndexes[1] - brushIndexes[0] + 1) * bw}
                  height={ih}
                  fill="var(--ink)"
                  opacity={brushOpen ? 0.24 : 0.12}
                  stroke="var(--ink)"
                  strokeWidth={brushOpen ? 2 : 1}
                  pointerEvents="none"
                />
              )}
            </svg>
            {shown && (
              <div
                role="tooltip"
                className="pointer-events-none absolute top-3 z-5 min-w-40 rounded-lg bg-raised px-[11px] py-[9px] text-xs shadow-pop"
                style={{ left: tipLeft }}
              >
                <b className="mb-1 block font-bold">{spanLabel(shown.start, shown.end)} UTC</b>
                <TipRow label="Success" value={shown.success} swatch="bg-success" />
                <TipRow label="Failure" value={shown.failure} swatch="bg-failure" />
                <TipRow label="Retry" value={shown.retry} swatch="retry-hatch" />
                <div className="flex items-center gap-1.5">
                  <span className="text-muted">Failure rate</span>
                  <span className="ml-auto font-semibold">
                    {formatPercent(shown.outcomes ? shown.failure / shown.outcomes : 0)}
                  </span>
                </div>
              </div>
            )}
          </>
        )}
      </div>
    </section>
  )
}

function Legend({ swatch, label }: { swatch: string; label: string }): ReactElement {
  return (
    <span className="inline-flex items-center gap-1.5">
      <i className={`inline-block size-2.5 rounded-[2px] ${swatch}`} />
      {label}
    </span>
  )
}

function TipRow({
  label,
  value,
  swatch,
}: {
  label: string
  value: number
  swatch: string
}): ReactElement {
  return (
    <div className="flex items-center gap-1.5">
      <i className={`inline-block size-2.5 rounded-[2px] ${swatch}`} />
      <span>{label}</span>
      <span className="ml-auto font-semibold">{formatCount(value)}</span>
    </div>
  )
}

function ChartTable({ buckets }: { buckets: Bucket[] }): ReactElement {
  const panel = usePanel()
  return (
    <div className="max-h-[300px] overflow-auto">
      <table className="w-full border-collapse text-xs">
        <caption className="sr-only">Runs per bucket</caption>
        <thead>
          <tr>
            {['Bucket (UTC)', 'Success', 'Failure', 'Retry', 'Failure rate'].map((h, i) => (
              <th
                key={h}
                scope="col"
                className={`sticky top-0 border-b border-rule bg-surface px-2.5 py-2 text-xs font-semibold text-muted ${i ? 'text-right' : 'text-left'}`}
              >
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {[...buckets].reverse().map((b) => (
            <tr key={b.start} className="border-b border-rule last:border-b-0 hover:bg-hover">
              <td className="px-2.5 py-1">
                <button
                  type="button"
                  {...openerProps(panel, { kind: 'runs', id: encodeScope(barScope(b)) })}
                  className="font-semibold hover:underline data-[on]:underline"
                >
                  {spanLabel(b.start, b.end)}
                </button>
              </td>
              <td className="px-2.5 py-1 text-right tabular-nums">{formatCount(b.success)}</td>
              <td
                className={`px-2.5 py-1 text-right tabular-nums ${b.failure ? 'font-bold text-failure-t' : ''}`}
              >
                {formatCount(b.failure)}
              </td>
              <td className="px-2.5 py-1 text-right tabular-nums">{formatCount(b.retry)}</td>
              <td className="px-2.5 py-1 text-right tabular-nums">
                {formatPercent(b.outcomes ? b.failure / b.outcomes : 0)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

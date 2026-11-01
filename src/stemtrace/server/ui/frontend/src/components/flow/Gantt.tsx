import {
  type ReactElement,
  type RefObject,
  useId,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from 'react'
import type { GraphNode } from '@/api/client'
import { IconGlyph } from '@/components/Icon'
import { stateLook } from '@/components/StatusChip'
import { formatSpan } from '@/utils/format'
import { activate, shortNamesOf, toneOf, truncate } from './flowFacts'
import { type FlowLayout, lastNameSegment } from './layoutFlow'
import { type Bar, MAX_DEPTH, type Timeline, type TimelineRow } from './timeline'

const ROW_H = 24
const AXIS_H = 26
/** Narrowest drawing: below this the panel scrolls inside itself, so labels stay 12 px. */
const MIN_W = 560
const NARROW_W = 760
const LABEL_W_NARROW = 170
const LABEL_W = 250
const PAD_RIGHT = 18
/** Characters of a label that fit per pixel of the label column (12 px sans). */
const CHAR_W = 6.6
const MIN_BAR_W = 3

interface GanttProps {
  timeline: Timeline
  nodes: Record<string, GraphNode>
  layout: FlowLayout
  /** The chosen node: its row is highlighted. */
  selectedId: string
  /** A row was chosen: select `id`, and centre `centreId` in the diagram. */
  onSelect: (id: string, centreId: string) => void
  onToggleExpanded: (containerId: string) => void
}

/** How long a bar waited and ran, in words, for a row's accessible name and tooltip. */
function timingWords(bar: Bar | null): string {
  if (!bar) return 'no timing'
  const waited = bar.leadTo - bar.from
  const wait = waited > 0 ? `waited ${formatSpan(waited)}` : ''
  if (!bar.hasRun) return bar.open ? 'queued' : wait || 'never started'
  const ran = bar.open ? 'running' : `ran ${formatSpan(bar.end - bar.runFrom)}`
  const tries =
    bar.runFrom > bar.leadTo
      ? `after ${formatSpan(bar.runFrom - bar.leadTo)} of earlier attempts`
      : ''
  return [wait, tries, ran].filter(Boolean).join(', ')
}

/** The container's width, tracked so the drawing is never scaled (a scaled label breaks 12 px). */
function useBoxWidth(): [RefObject<HTMLDivElement | null>, number] {
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

/**
 * The Timeline: a row per task on a shared time axis, bars from the Timeline model (lead-in,
 * earlier attempts, last run), the critical path outlined and joined, and rows that choose a
 * node, or expand a container for "+N more". Drawn at natural size: when the panel is narrower
 * than the drawing it scrolls inside itself, so the page never does.
 */
export function Gantt({
  timeline,
  nodes,
  layout,
  selectedId,
  onSelect,
  onToggleExpanded,
}: GanttProps): ReactElement {
  const headingId = useId()
  const hatchRun = useId()
  const hatchTries = useId()
  const [boxRef, boxWidth] = useBoxWidth()
  const shortNames = useMemo(() => shortNamesOf(nodes), [nodes])
  const containers = new Map(layout.containers.map((box) => [box.id, box]))
  const placed = new Map(layout.tasks.map((task) => [task.id, task]))
  const { axis, rows } = timeline

  const width = Math.max(MIN_W, boxWidth)
  const labelW = width < NARROW_W ? LABEL_W_NARROW : LABEL_W
  const plotW = width - labelW - PAD_RIGHT
  const height = rows.length * ROW_H + 8
  const x = (t: number): number =>
    axis ? labelW + 8 + ((t - axis.t0) / (axis.t1 - axis.t0)) * plotW : labelW + 8

  const yMid = new Map<string, number>()
  rows.forEach((row, index) => {
    if (row.bar?.hasRun) {
      yMid.set(row.kind === 'more' ? `${row.id}:more` : row.id, index * ROW_H + ROW_H / 2)
    }
  })
  const joins = timeline.path
    .filter((id) => yMid.has(id))
    .flatMap((id, index, ids) => (index === 0 ? [] : [[ids[index - 1], id] as const]))

  return (
    <section aria-labelledby={headingId} className="min-w-0 rounded-lg bg-surface">
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1 px-4 pt-3.5 pb-2">
        <h2 id={headingId} className="m-0 text-sm font-bold text-ink">
          Timeline
        </h2>
        <span className="text-xs text-muted">
          Bars start when each task started running. The grey lead-in is queue wait; a dotted one is
          a countdown or ETA. Outlined bars are the critical path.
        </span>
      </div>
      {axis === null ? (
        <p className="m-0 px-4 pt-2 pb-6 text-muted">No timing data yet.</p>
      ) : (
        <div ref={boxRef} className="gantt-box" data-testid="gantt-box">
          <svg
            className="gantt-axis"
            width={width}
            height={AXIS_H}
            aria-hidden="true"
            focusable="false"
          >
            {axis.ticks.map((tick) => (
              <g key={tick.t}>
                <line
                  className="gantt-grid"
                  x1={x(tick.t)}
                  x2={x(tick.t)}
                  y1={AXIS_H - 6}
                  y2={AXIS_H}
                />
                <text className="gantt-m" x={x(tick.t) + 4} y={17}>
                  {tick.label}
                </text>
              </g>
            ))}
          </svg>
          <svg
            className="gantt"
            width={width}
            height={height}
            role="group"
            aria-label="Timeline of task start times and durations"
          >
            <defs>
              <pattern
                id={hatchRun}
                width="6"
                height="6"
                patternUnits="userSpaceOnUse"
                patternTransform="rotate(45)"
              >
                <rect width="6" height="6" style={{ fill: 'var(--running)' }} />
                <rect width="2.5" height="6" style={{ fill: 'var(--surface)' }} />
              </pattern>
              <pattern
                id={hatchTries}
                width="6"
                height="6"
                patternUnits="userSpaceOnUse"
                patternTransform="rotate(45)"
              >
                <rect width="6" height="6" style={{ fill: 'var(--retry)' }} />
                <rect width="2.5" height="6" style={{ fill: 'var(--surface)' }} />
              </pattern>
            </defs>
            {axis.ticks.map((tick) => (
              <line
                key={tick.t}
                className="gantt-grid"
                x1={x(tick.t)}
                x2={x(tick.t)}
                y1={0}
                y2={height}
              />
            ))}
            {rows.map((row, index) => (
              <GanttRow
                key={row.key}
                row={row}
                y={index * ROW_H}
                width={width}
                labelW={labelW}
                x={x}
                node={nodes[row.id]}
                label={rowLabel(row, nodes, containers, placed, shortNames)}
                mono={rowMono(row, containers, placed)}
                detail={detailOf(row, layout)}
                selected={row.kind !== 'more' && selectedId === row.id}
                hatchRun={hatchRun}
                hatchTries={hatchTries}
                onSelect={onSelect}
                onToggleExpanded={onToggleExpanded}
              />
            ))}
            {joins.map(([from, to]) => {
              const a = rows.find(
                (row) => (row.kind === 'more' ? `${row.id}:more` : row.id) === from,
              )
              const b = rows.find((row) => (row.kind === 'more' ? `${row.id}:more` : row.id) === to)
              if (!a?.bar || !b?.bar) return null
              const x1 = x(a.bar.end)
              const x2 = x(b.bar.runFrom)
              const mid = (x1 + x2) / 2
              return (
                <path
                  key={`${from}>${to}`}
                  className="gantt-cpl"
                  d={`M${x1},${yMid.get(from)} H${mid} V${yMid.get(to)} H${x2}`}
                />
              )
            })}
          </svg>
        </div>
      )}
    </section>
  )
}

/** A task's row text: its short name, or the start of its ID inside a container of one task name. */
function rowMono(
  row: TimelineRow,
  containers: Map<string, { sharedName: string | null }>,
  placed: Map<string, { containerId: string | null }>,
): boolean {
  if (row.kind !== 'task') return false
  const owner = placed.get(row.id)?.containerId
  return Boolean(owner && containers.get(owner)?.sharedName)
}

function rowLabel(
  row: TimelineRow,
  nodes: Record<string, GraphNode>,
  containers: Map<string, { sharedName: string | null }>,
  placed: Map<string, { containerId: string | null }>,
  shortNames: Map<string, string>,
): string {
  if (row.kind === 'more') return `+${row.hidden} more`
  if (row.kind === 'notRun') return 'Callback'
  const node = nodes[row.id]
  if (!node) return row.id
  if (rowMono(row, containers, placed)) return row.id.slice(0, 8)
  return shortNames.get(node.name) ?? lastNameSegment(node.name)
}

/** The words after the label: a "+N more" row's summary, a not-run row's reason. */
function detailOf(row: TimelineRow, layout: FlowLayout): string {
  if (row.kind === 'more') {
    return layout.more.find((b) => b.containerId === row.id && b.kind === 'more')?.summary ?? ''
  }
  if (row.kind === 'notRun') return layout.notRun.find((b) => b.chordId === row.id)?.text ?? ''
  return ''
}

interface GanttRowProps {
  row: TimelineRow
  y: number
  width: number
  labelW: number
  x: (t: number) => number
  node: GraphNode | undefined
  label: string
  mono: boolean
  detail: string
  selected: boolean
  hatchRun: string
  hatchTries: string
  onSelect: (id: string, centreId: string) => void
  onToggleExpanded: (containerId: string) => void
}

function GanttRow({
  row,
  y,
  width,
  labelW,
  x,
  node,
  label,
  mono,
  detail,
  selected,
  hatchRun,
  hatchTries,
  onSelect,
  onToggleExpanded,
}: GanttRowProps): ReactElement {
  const look = node ? stateLook(node.state) : null
  const tone = row.kind === 'task' && node ? toneOf(node.state) : 'pending'
  const indent = Math.min(row.depth, MAX_DEPTH) * 8
  const bar = row.bar
  const critical = row.critical
  const named =
    row.kind === 'more'
      ? `+${row.hidden} more: ${detail}. Click to expand.`
      : row.kind === 'notRun'
        ? `Callback not run, ${detail}`
        : [
            `${node?.name ?? row.id}${mono ? ` ${row.id.slice(0, 8)}` : ''}`,
            look?.word,
            timingWords(bar),
          ]
            .filter(Boolean)
            .join(', ')
  const accessibleName = critical ? `${named}, on the critical path` : named
  const act = (): void => {
    if (row.kind === 'more') onToggleExpanded(row.id)
    else onSelect(row.id, row.centreId)
  }
  const room = Math.floor((labelW - 36 - indent - (critical ? 70 : 0)) / CHAR_W)
  const mid = y + ROW_H / 2
  const run = bar?.hasRun ? bar : null
  const runX = run ? x(run.runFrom) : 0
  const runW = run ? Math.max(x(run.end) - runX, MIN_BAR_W) : 0
  const retried = run !== null && run.runFrom > run.leadTo

  return (
    <g
      className={`gantt-row s-${tone}${selected ? ' sel' : ''}`}
      data-testid="gantt-row"
      data-row-id={row.id}
      data-row-kind={row.kind}
      data-critical={critical ? 'true' : undefined}
      data-state={node?.state}
      tabIndex={0}
      role="button"
      aria-label={accessibleName}
      aria-pressed={row.kind === 'more' ? undefined : selected}
      onClick={act}
      onKeyDown={activate(act)}
    >
      <title>{accessibleName}</title>
      <rect className="gantt-bg" x={0} y={y} width={width} height={ROW_H} />
      <rect className="gantt-mark" x={8 + indent} y={y + 4} width={16} height={16} rx={3} />
      <IconGlyph
        className="gantt-ico"
        name={
          row.kind === 'more' ? 'plus' : row.kind === 'notRun' ? 'clock' : (look?.icon ?? 'clock')
        }
        x={10 + indent}
        y={y + 6}
        size={12}
      />
      <text
        className={`${critical ? 'gantt-cp' : ''}${mono ? ' gantt-id' : ''}`}
        x={30 + indent}
        y={y + 16}
      >
        {truncate(label, Math.max(room, 4))}
      </text>
      {critical && (
        <text className="gantt-m gantt-cp" x={labelW - 6} y={y + 16} textAnchor="end">
          critical
        </text>
      )}
      <line className="gantt-rl" x1={labelW + 8} x2={width - 10} y1={mid} y2={mid} />
      {bar && (
        <>
          {bar.scheduledTo > bar.from && (
            <path className="gantt-sched" d={`M${x(bar.from)},${mid} H${x(bar.scheduledTo)}`} />
          )}
          {bar.leadTo > bar.scheduledTo && (
            <rect
              className="gantt-wait"
              x={x(bar.scheduledTo)}
              y={y + 9}
              width={Math.max(x(bar.leadTo) - x(bar.scheduledTo), 0)}
              height={6}
              rx={3}
            />
          )}
          {retried && (
            <rect
              className="gantt-tries"
              x={x(bar.leadTo)}
              y={y + 5}
              width={Math.max(x(bar.runFrom) - x(bar.leadTo), 0)}
              height={ROW_H - 10}
              style={{ fill: `url(#${hatchTries})` }}
            />
          )}
          {run && (
            <rect
              className={`gantt-run${critical ? ' gantt-crit' : ''}`}
              x={runX}
              y={y + 5}
              width={runW}
              height={ROW_H - 10}
              rx={3}
              data-open={run.open ? 'true' : undefined}
              style={run.open ? { fill: `url(#${hatchRun})` } : undefined}
            />
          )}
        </>
      )}
      {row.kind === 'notRun' && (
        <>
          <path className="gantt-notrun" d={`M${labelW + 8},${mid} H${labelW + 8 + 150}`} />
          <text className="gantt-m" x={labelW + 16 + 150} y={y + 16}>
            not run
          </text>
        </>
      )}
      {row.kind === 'task' && !bar && (
        <text className="gantt-m" x={labelW + 16} y={y + 16}>
          no timing
        </text>
      )}
    </g>
  )
}

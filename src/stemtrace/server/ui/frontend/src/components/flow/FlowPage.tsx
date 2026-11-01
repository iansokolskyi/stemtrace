import { type ReactElement, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import type { GraphNode } from '@/api/client'
import { flowSummary, flowTitle } from '@/components/flows/flowSummary'
import { Icon } from '@/components/Icon'
import { StatusChip } from '@/components/StatusChip'
import { useSetCrumbTitle } from '@/hooks/useCrumbTitle'
import { FlowDiagram } from './FlowDiagram'
import { firstFailure, summarizeFlow } from './flowFacts'
import { Gantt } from './Gantt'
import { Inspector } from './Inspector'
import { layoutFlow } from './layoutFlow'
import { buildTimeline } from './timeline'

/** Zoom steps above Fit (natural size): 1.0 to 1.4 by 0.2, as the prototype. */
const ZOOM_STEP = 0.2
const ZOOM_MIN = 1
const ZOOM_MAX = 1.4

const BUTTON =
  'inline-flex h-[30px] items-center gap-1.5 rounded-[3px] px-3 text-xs font-semibold whitespace-nowrap bg-raised text-ink hover:bg-press disabled:opacity-45 disabled:hover:bg-raised'

const reducedMotion = (): boolean => {
  try {
    return window.matchMedia('(prefers-reduced-motion: reduce)').matches
  } catch {
    return false
  }
}

interface FlowPageProps {
  nodes: Record<string, GraphNode>
  rootId: string
}

/**
 * The Flow page: title, state and summary with Jump to failure and zoom, the track diagram, the
 * inspector and the Timeline. A Timeline row chooses and centres a node like Jump to failure. The first failure is chosen on open (the view stays at the root). Once the reader
 * has acted (zoomed, pressed Fit, chosen a node, expanded or collapsed, scrolled), a live update never moves the view;
 * until then a new node that is out of the box is scrolled into it.
 */
export function FlowPage({ nodes, rootId }: FlowPageProps): ReactElement {
  const [chosen, setChosen] = useState<string | null>(null)
  const [zoom, setZoom] = useState<number | null>(null)
  const [expanded, setExpanded] = useState<ReadonlySet<string>>(() => new Set())
  // Set by every reader action (choosing, zoom buttons including Fit, expand and collapse,
  // scrolling) and never cleared: undoing an action does not give the view back to live updates.
  // The page is keyed by the flow, so a new flow starts unlocked.
  const [acted, setActed] = useState(false)
  const boxRef = useRef<HTMLDivElement>(null)

  const layout = useMemo(() => layoutFlow(nodes, rootId, expanded), [nodes, rootId, expanded])
  const summary = useMemo(() => summarizeFlow(nodes, rootId), [nodes, rootId])
  const failure = useMemo(() => firstFailure(nodes), [nodes])
  const root = nodes[rootId]
  useSetCrumbTitle(root ? flowTitle(root.name) : undefined)
  const selected = chosen && nodes[chosen] ? chosen : (failure?.task_id ?? rootId)
  const scale = zoom ?? 1
  const locked = acted

  const timeline = useMemo(() => buildTimeline(nodes, layout, rootId), [nodes, layout, rootId])

  const rectOf = (id: string) =>
    layout.tasks.find((task) => task.id === id) ??
    layout.containers.find((box) => box.id === id) ??
    layout.notRun.find((box) => box.id === id)

  // The box's own scrolls (centring, zoom, scroll-into-view) are marked, so that any other
  // scroll, by whatever input (wheel, scrollbar, keys, focus moving to a node), is the reader's.
  const own = useRef<{ left: number; top: number; until: number } | null>(null)
  const scrollBoxTo = (left: number, top: number, behavior: ScrollBehavior): void => {
    const box = boxRef.current
    if (!box) return
    const target = {
      left: Math.round(Math.max(0, Math.min(left, box.scrollWidth - box.clientWidth))),
      top: Math.round(Math.max(0, Math.min(top, box.scrollHeight - box.clientHeight))),
    }
    if (Math.abs(box.scrollLeft - target.left) < 1 && Math.abs(box.scrollTop - target.top) < 1)
      return
    // A smooth scroll sends events on the way; the mark ends at the target or after a second
    own.current = { ...target, until: Date.now() + 1000 }
    box.scrollTo({ ...target, behavior })
  }
  const handleScroll = (): void => {
    const box = boxRef.current
    const mark = own.current
    if (box && mark && Date.now() < mark.until) {
      if (Math.abs(box.scrollLeft - mark.left) < 1 && Math.abs(box.scrollTop - mark.top) < 1) {
        own.current = null
      }
      return
    }
    own.current = null
    setActed(true)
  }

  const centreOn = (id: string): void => {
    const box = boxRef.current
    const rect = rectOf(id)
    if (!box || !rect) return
    scrollBoxTo(
      (rect.x + rect.w / 2) * scale - box.clientWidth / 2,
      (rect.y + rect.h / 2) * scale - box.clientHeight / 2,
      reducedMotion() ? 'auto' : 'smooth',
    )
  }

  // Zooming keeps what is in the middle of the box in the middle (a render at the same scale
  // does nothing)
  const shownScale = useRef(scale)
  useLayoutEffect(() => {
    const box = boxRef.current
    const before = shownScale.current
    shownScale.current = scale
    if (!box || before === scale) return
    // Whole pixels: a fractional offset is snapped again when the content changes size
    scrollBoxTo(
      ((box.scrollLeft + box.clientWidth / 2) / before) * scale - box.clientWidth / 2,
      ((box.scrollTop + box.clientHeight / 2) / before) * scale - box.clientHeight / 2,
      'auto',
    )
  })

  // A node that arrives while the reader has not moved the view is scrolled into the box
  // No dependency array on purpose: it compares the nodes drawn now with those of the last render,
  // so a render with nothing new does nothing, and `locked` and `scale` are always current.
  const drawn = useRef<Set<string> | null>(null)
  useEffect(() => {
    const ids = new Set([
      ...layout.tasks.map((task) => task.id),
      ...layout.containers.map((box) => box.id),
    ])
    const before = drawn.current
    drawn.current = ids
    const box = boxRef.current
    if (!before || !box || locked) return
    const added = [...ids].filter((id) => !before.has(id)).flatMap((id) => rectOf(id) ?? [])
    if (added.length === 0) return
    const left = Math.min(...added.map((rect) => rect.x)) * scale
    const top = Math.min(...added.map((rect) => rect.y)) * scale
    const right = Math.max(...added.map((rect) => rect.x + rect.w)) * scale
    const bottom = Math.max(...added.map((rect) => rect.y + rect.h)) * scale
    const margin = 16
    let nextLeft = box.scrollLeft
    let nextTop = box.scrollTop
    if (right > nextLeft + box.clientWidth) nextLeft = right - box.clientWidth + margin
    if (left < nextLeft) nextLeft = left - margin
    if (bottom > nextTop + box.clientHeight) nextTop = bottom - box.clientHeight + margin
    if (top < nextTop) nextTop = top - margin
    scrollBoxTo(nextLeft, nextTop, 'auto')
  })

  const toggleExpanded = (containerId: string): void => {
    setActed(true)
    setExpanded((current) => {
      const next = new Set(current)
      if (!next.delete(containerId)) next.add(containerId)
      return next
    })
  }

  const zoomBy = (delta: number): void => {
    setActed(true)
    setZoom((current) => {
      const next = Math.round(((current ?? 1) + delta) * 10) / 10
      return Math.max(ZOOM_MIN, Math.min(ZOOM_MAX, next))
    })
  }

  if (!root || layout.tasks.length + layout.containers.length === 0) {
    return <p className="py-12 text-center text-muted">No nodes in graph</p>
  }

  return (
    <div>
      <header className="mb-4 flex flex-wrap items-center gap-x-3 gap-y-2">
        <h1 className="m-0 min-w-0 text-2xl font-bold text-ink [overflow-wrap:anywhere]">
          {flowTitle(root.name)}
        </h1>
        <StatusChip state={summary.state} />
        <span className="text-muted">{flowSummary(summary)}</span>
        <span className="grow" />
        <button
          type="button"
          className={BUTTON}
          disabled={!failure}
          title={failure ? undefined : 'No failed task in this flow'}
          onClick={() => {
            if (!failure) return
            setActed(true)
            setChosen(failure.task_id)
            centreOn(failure.task_id)
          }}
        >
          <Icon name="jump" size={13} />
          Jump to failure
        </button>
        <div
          role="group"
          aria-label="Zoom"
          className="inline-flex gap-px overflow-hidden rounded-[3px]"
        >
          <button
            type="button"
            className={`${BUTTON} rounded-none px-2.5`}
            aria-label="Zoom out"
            disabled={zoom === null || zoom <= ZOOM_MIN}
            onClick={() => zoomBy(-ZOOM_STEP)}
          >
            <Icon name="minus" size={12} />
          </button>
          <button
            type="button"
            className={`${BUTTON} min-w-[52px] justify-center rounded-none`}
            aria-pressed={zoom === null}
            title="Show the graph at its natural size"
            onClick={() => {
              setActed(true)
              setZoom(null)
            }}
          >
            {zoom === null ? 'Fit' : `${Math.round(zoom * 100)}%`}
          </button>
          <button
            type="button"
            className={`${BUTTON} rounded-none px-2.5`}
            aria-label="Zoom in"
            disabled={zoom !== null && zoom >= ZOOM_MAX}
            onClick={() => zoomBy(ZOOM_STEP)}
          >
            <Icon name="plus" size={12} />
          </button>
        </div>
      </header>

      <div className="mb-4 grid min-w-0 items-start gap-4 min-[1280px]:grid-cols-[minmax(0,1fr)_260px] min-[1440px]:grid-cols-[minmax(0,1fr)_330px]">
        <FlowDiagram
          layout={layout}
          nodes={nodes}
          selectedId={selected}
          scale={scale}
          boxRef={boxRef}
          onSelect={(id) => {
            setActed(true)
            setChosen(id)
          }}
          onToggleExpanded={toggleExpanded}
          onScroll={handleScroll}
        />
        <Inspector
          nodes={nodes}
          layout={layout}
          selectedId={selected}
          onToggleExpanded={toggleExpanded}
        />
      </div>

      <Gantt
        timeline={timeline}
        nodes={nodes}
        layout={layout}
        selectedId={selected}
        onSelect={(id, centreId) => {
          setActed(true)
          setChosen(id)
          centreOn(centreId)
        }}
        onToggleExpanded={toggleExpanded}
      />
    </div>
  )
}

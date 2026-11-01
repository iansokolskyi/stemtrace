import { type ReactElement, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import type { GraphNode } from '@/api/client'
import { flowSummary, flowTitle } from '@/components/flows/flowSummary'
import { Icon } from '@/components/Icon'
import { StatusChip } from '@/components/StatusChip'
import { useSetCrumbTitle } from '@/hooks/useCrumbTitle'
import { FlowDiagram } from './FlowDiagram'
import { firstFailure, summarizeFlow } from './flowFacts'
import { Inspector } from './Inspector'
import { layoutFlow } from './layoutFlow'

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
 * The Flow page: title, state and summary with Jump to failure and zoom, the track diagram and
 * the inspector. The first failure is chosen on open (the view stays at the root). Once the reader
 * has zoomed, chosen a node, expanded a container or scrolled, a live update never moves the view;
 * until then a new node that is out of the box is scrolled into it.
 */
export function FlowPage({ nodes, rootId }: FlowPageProps): ReactElement {
  const [chosen, setChosen] = useState<string | null>(null)
  const [zoom, setZoom] = useState<number | null>(null)
  const [expanded, setExpanded] = useState<ReadonlySet<string>>(() => new Set())
  const [moved, setMoved] = useState(false)
  const boxRef = useRef<HTMLDivElement>(null)

  const layout = useMemo(() => layoutFlow(nodes, rootId, expanded), [nodes, rootId, expanded])
  const summary = useMemo(() => summarizeFlow(nodes, rootId), [nodes, rootId])
  const failure = useMemo(() => firstFailure(nodes), [nodes])
  const root = nodes[rootId]
  useSetCrumbTitle(root ? flowTitle(root.name) : undefined)
  const selected = chosen && nodes[chosen] ? chosen : (failure?.task_id ?? rootId)
  const scale = zoom ?? 1
  const locked = chosen !== null || zoom !== null || moved

  const rectOf = (id: string) =>
    layout.tasks.find((task) => task.id === id) ?? layout.containers.find((box) => box.id === id)

  const centreOn = (id: string): void => {
    const box = boxRef.current
    const rect = rectOf(id)
    if (!box || !rect) return
    box.scrollTo({
      left: Math.max(0, (rect.x + rect.w / 2) * scale - box.clientWidth / 2),
      top: Math.max(0, (rect.y + rect.h / 2) * scale - box.clientHeight / 2),
      behavior: reducedMotion() ? 'auto' : 'smooth',
    })
  }

  // Zooming keeps what is in the middle of the box in the middle
  const shownScale = useRef(scale)
  useLayoutEffect(() => {
    const box = boxRef.current
    const before = shownScale.current
    shownScale.current = scale
    if (!box || before === scale) return
    // Whole pixels: a fractional offset is snapped again when the content changes size
    box.scrollLeft = Math.max(
      0,
      Math.round(((box.scrollLeft + box.clientWidth / 2) / before) * scale - box.clientWidth / 2),
    )
    box.scrollTop = Math.max(
      0,
      Math.round(((box.scrollTop + box.clientHeight / 2) / before) * scale - box.clientHeight / 2),
    )
  }, [scale])

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
    if (right > box.scrollLeft + box.clientWidth) box.scrollLeft = right - box.clientWidth + margin
    if (left < box.scrollLeft) box.scrollLeft = Math.max(0, left - margin)
    if (bottom > box.scrollTop + box.clientHeight)
      box.scrollTop = bottom - box.clientHeight + margin
    if (top < box.scrollTop) box.scrollTop = Math.max(0, top - margin)
  })

  const toggleExpanded = (containerId: string): void => {
    setMoved(true)
    setExpanded((current) => {
      const next = new Set(current)
      if (!next.delete(containerId)) next.add(containerId)
      return next
    })
  }

  const zoomBy = (delta: number): void => {
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
            onClick={() => setZoom(null)}
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
          onSelect={setChosen}
          onToggleExpanded={toggleExpanded}
          onUserMove={() => setMoved(true)}
        />
        <Inspector
          nodes={nodes}
          layout={layout}
          selectedId={selected}
          onToggleExpanded={toggleExpanded}
        />
      </div>
    </div>
  )
}

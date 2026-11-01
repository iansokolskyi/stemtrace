import { type KeyboardEvent, type ReactElement, type Ref, useId } from 'react'
import type { GraphNode } from '@/api/client'
import { IconGlyph } from '@/components/Icon'
import { stateLook } from '@/components/StatusChip'
import { formatSpan } from '@/utils/format'
import { taskSubtitle, toneOf, truncate, wrapTwo } from './flowFacts'
import { type FlowLayout, lastNameSegment, type PlacedContainer } from './layoutFlow'
import { trackPath } from './trackPath'

interface FlowDiagramProps {
  layout: FlowLayout
  nodes: Record<string, GraphNode>
  selectedId: string
  /** Drawing size over the natural size: 1 is Fit. */
  scale: number
  boxRef: Ref<HTMLDivElement>
  onSelect: (id: string) => void
  onToggleExpanded: (containerId: string) => void
  /** The reader scrolled or moved through the box themselves. */
  onUserMove: () => void
}

type Item =
  | { kind: 'task'; seq: number; id: string }
  | { kind: 'container'; seq: number; id: string }
  | { kind: 'more'; seq: number; index: number }
  | { kind: 'notRun'; seq: number; index: number }

/** Enter or Space does what a click does on a focusable part of the drawing. */
function activate(action: () => void): (event: KeyboardEvent) => void {
  return (event) => {
    if (event.key !== 'Enter' && event.key !== ' ') return
    event.preventDefault()
    action()
  }
}

/**
 * The flow as a track diagram drawn in SVG, left to right, inside a box that scrolls both ways.
 * Every task, container, "+N more" block and not-run box can be focused (tab order is the order
 * the layout reached them) and chosen with a click, Enter or Space. A state is told by icon and
 * word in a block's accessible name, never by colour alone. The `data-*` hooks are what the tests
 * read (tests/fixtures/flow.ts).
 */
export function FlowDiagram({
  layout,
  nodes,
  selectedId,
  scale,
  boxRef,
  onSelect,
  onToggleExpanded,
  onUserMove,
}: FlowDiagramProps): ReactElement {
  const hatch = useId()
  const containers = new Map(layout.containers.map((box) => [box.id, box]))

  const items: Item[] = [
    ...layout.tasks.map((task): Item => ({ kind: 'task', seq: task.seq, id: task.id })),
    ...layout.containers.map((box): Item => ({ kind: 'container', seq: box.seq, id: box.id })),
    ...layout.more.map((block, index): Item => ({ kind: 'more', seq: block.seq, index })),
    ...layout.notRun.map((box, index): Item => ({ kind: 'notRun', seq: box.seq, index })),
  ].sort((a, b) => a.seq - b.seq)
  const placedTasks = new Map(layout.tasks.map((task) => [task.id, task]))

  return (
    <div
      ref={boxRef}
      data-testid="flow-canvas"
      role="region"
      aria-label="Flow graph, scrollable"
      tabIndex={0}
      className="flow-box"
      onWheel={onUserMove}
      onTouchMove={onUserMove}
      onKeyDown={(event) => {
        if (event.target === event.currentTarget && event.key.startsWith('Arrow')) onUserMove()
      }}
    >
      <svg
        width={layout.width * scale}
        height={layout.height * scale}
        viewBox={`0 0 ${layout.width} ${layout.height}`}
        preserveAspectRatio="xMinYMin meet"
        role="group"
        aria-label="Flow graph drawn as a track diagram, left to right"
      >
        <defs>
          <pattern
            id={hatch}
            width="5"
            height="5"
            patternUnits="userSpaceOnUse"
            patternTransform="rotate(45)"
          >
            <rect width="5" height="5" style={{ fill: 'var(--surface)' }} />
            <rect width="3" height="5" style={{ fill: 'var(--retry)' }} />
          </pattern>
        </defs>

        {layout.containers.map((box) => (
          <rect
            key={box.id}
            className="flow-cbox"
            x={box.x}
            y={box.y}
            width={box.w}
            height={box.h}
            rx={8}
          />
        ))}

        {layout.containers.map((box) => (
          <ContainerTracks key={box.id} box={box} />
        ))}
        {layout.edges.map((edge) => (
          <g
            key={`${edge.from}>${edge.to}:${edge.x1},${edge.y1}`}
            data-testid="flow-edge"
            data-from={edge.from}
            data-to={edge.to}
          >
            <path
              className={edge.notRun ? 'flow-track flow-track-nr' : 'flow-track'}
              d={
                edge.junction
                  ? `${trackPath(edge.junction.x, edge.junction.y, edge.x1, edge.y1)} ${trackPath(
                      edge.junction.x,
                      edge.junction.y,
                      edge.x2,
                      edge.y2,
                    )}`
                  : trackPath(edge.x1, edge.y1, edge.x2, edge.y2)
              }
            />
            {edge.failed && (
              <path
                className="flow-track flow-track-bad"
                d={
                  edge.junction
                    ? trackPath(edge.junction.x, edge.junction.y, edge.x1, edge.y1, true)
                    : trackPath(edge.x1, edge.y1, edge.x2, edge.y2, edge.fan)
                }
              />
            )}
          </g>
        ))}

        {items.map((item) => {
          if (item.kind === 'container') {
            const box = containers.get(item.id) as PlacedContainer
            return (
              <ContainerNode
                key={`c:${item.id}`}
                box={box}
                node={nodes[item.id]}
                selected={selectedId === item.id}
                onSelect={onSelect}
              />
            )
          }
          if (item.kind === 'task') {
            const placed = placedTasks.get(item.id)
            if (!placed) return null
            const container = placed.containerId ? containers.get(placed.containerId) : undefined
            return (
              <TaskBlock
                key={`t:${item.id}`}
                placed={placed}
                node={nodes[item.id]}
                shortLabel={container?.sharedName ? item.id.slice(0, 8) : null}
                hatch={hatch}
                selected={selectedId === item.id}
                onSelect={onSelect}
              />
            )
          }
          if (item.kind === 'more') {
            const block = layout.more[item.index]
            const label =
              block.kind === 'more'
                ? `+${block.hiddenCount} more: ${block.summary}. Click to expand.`
                : 'Collapse back to the first 3 and any failed.'
            const toggle = () => onToggleExpanded(block.containerId)
            return (
              <g
                key={`m:${block.containerId}:${block.kind}`}
                className="flow-btn"
                data-testid="flow-more"
                data-container-id={block.containerId}
                data-kind={block.kind}
                transform={`translate(${block.x},${block.y})`}
                tabIndex={0}
                role="button"
                aria-label={label}
                onClick={toggle}
                onKeyDown={activate(toggle)}
              >
                <title>{label}</title>
                <rect
                  className="ring"
                  x={-4}
                  y={-4}
                  width={block.w + 8}
                  height={block.h + 8}
                  rx={6}
                />
                <rect className="fb" width={block.w} height={block.h} rx={3} />
                {block.kind === 'more' ? (
                  <text x={12} y={21}>
                    <tspan className="t1">+{block.hiddenCount} more</tspan>
                    <tspan dx={8}>{truncate(block.summary, 14)}</tspan>
                  </text>
                ) : (
                  <text className="t1" x={12} y={21}>
                    Collapse
                  </text>
                )}
                <IconGlyph
                  name={block.kind === 'more' ? 'down' : 'up'}
                  x={block.w - 24}
                  y={9}
                  size={14}
                />
              </g>
            )
          }
          const box = layout.notRun[item.index]
          const [first, second] = wrapTwo(box.text, 22)
          const label = `Callback not run, ${box.text}`
          return (
            <g
              key={`n:${box.id}`}
              className="flow-node-g fn nr"
              data-testid="flow-not-run"
              data-chord-id={box.chordId}
              data-reason={box.reason}
              transform={`translate(${box.x},${box.y})`}
              tabIndex={0}
              role="button"
              aria-label={label}
              onClick={() => onSelect(box.chordId)}
              onKeyDown={activate(() => onSelect(box.chordId))}
            >
              <title>{label}</title>
              <rect className="ring" x={-4} y={-4} width={box.w + 8} height={box.h + 8} rx={6} />
              <rect className="blk" width={box.w} height={box.h} rx={3} />
              <IconGlyph name="clock" x={12} y={10} size={14} />
              <text className="t1" x={34} y={21}>
                Callback
              </text>
              <text className="t2" x={34} y={38}>
                not run
              </text>
              <text className="t2" x={34} y={54}>
                {first}
              </text>
              <text className="t2" x={34} y={69}>
                {second}
              </text>
            </g>
          )
        })}
      </svg>
    </div>
  )
}

/** A container's tracks: split at its left edge, one per member row, rejoined at its right edge. */
function ContainerTracks({ box }: { box: PlacedContainer }): ReactElement {
  const left = box.x + 20
  const right = box.x + box.w - 20
  return (
    <g>
      {box.tracks.map((track) => (
        <g key={track.y}>
          <path className="flow-track" d={trackPath(box.x, box.hub, left, track.y)} />
          <path className="flow-track" d={`M${left},${track.y} H${right}`} />
          {!track.railOut && (
            <path className="flow-track" d={trackPath(box.x + box.w, box.hub, right, track.y)} />
          )}
          {track.failed && (
            <>
              <path
                className="flow-track flow-track-bad"
                d={trackPath(box.x, box.hub, left, track.y, true)}
              />
              <path className="flow-track flow-track-bad" d={`M${left},${track.y} H${right}`} />
              {!track.railOut && (
                <path
                  className="flow-track flow-track-bad"
                  d={trackPath(box.x + box.w, box.hub, right, track.y, true)}
                />
              )}
            </>
          )}
        </g>
      ))}
    </g>
  )
}

interface ContainerNodeProps {
  box: PlacedContainer
  node: GraphNode
  selected: boolean
  onSelect: (id: string) => void
}

/** A GROUP or CHORD: a hit area over the whole box, its state mark and its title. */
function ContainerNode({ box, node, selected, onSelect }: ContainerNodeProps): ReactElement {
  const look = stateLook(node.state)
  const kind = box.type === 'CHORD' ? 'Chord' : 'Group'
  const title = `${kind} of ${box.memberCount} ${box.sharedName ?? 'tasks'}`
  const counts =
    `${box.succeeded} succeeded` +
    (box.failed > 0 ? `, ${box.failed} failed` : '') +
    (box.other > 0 ? `, ${box.other} not done` : '')
  const label = `${title}, ${look.word}, ${counts}`
  return (
    <g
      className={`fn fcont s-${toneOf(node.state)}${selected ? ' sel' : ''}`}
      data-testid="flow-node"
      data-task-id={box.id}
      data-node-type={box.type}
      data-state={node.state}
      tabIndex={0}
      role="button"
      aria-label={label}
      aria-pressed={selected}
      onClick={() => onSelect(box.id)}
      onKeyDown={activate(() => onSelect(box.id))}
    >
      <title>{label}</title>
      <desc>{node.state}</desc>
      <rect
        className="ring"
        x={box.x - 4}
        y={box.y - 4}
        width={box.w + 8}
        height={box.h + 8}
        rx={12}
      />
      <rect className="hit" x={box.x} y={box.y} width={box.w} height={box.h} rx={8} />
      <g data-testid="flow-container-label" data-container-id={box.id}>
        <desc>{box.type}</desc>
        <rect className="cmk" x={box.x + 14} y={box.y + 9} width={22} height={22} rx={3} />
        <IconGlyph name={look.icon} x={box.x + 18} y={box.y + 13} size={14} />
        <text className="ctitle" x={box.x + 44} y={box.y + 20}>
          {truncate(title, 24)}
        </text>
        <text className="ctitle c2" x={box.x + 44} y={box.y + 36}>
          {box.succeeded} succeeded
          {box.failed > 0 && (
            <>
              , <tspan className="bad">{box.failed} failed</tspan>
            </>
          )}
          {box.other > 0 && `, ${box.other} not done`}
        </text>
      </g>
    </g>
  )
}

interface TaskBlockProps {
  placed: { id: string; x: number; y: number; w: number; h: number; containerId: string | null }
  node: GraphNode
  /** A member of a container whose members share a name: its ID's start tells them apart. */
  shortLabel: string | null
  hatch: string
  selected: boolean
  onSelect: (id: string) => void
}

/** A task lit by its state: icon, last name segment and a line of facts (a member: name and runtime). */
function TaskBlock({
  placed,
  node,
  shortLabel,
  hatch,
  selected,
  onSelect,
}: TaskBlockProps): ReactElement {
  const look = stateLook(node.state)
  const tone = toneOf(node.state)
  const name = lastNameSegment(node.name)
  const member = placed.containerId !== null
  const subtitle = taskSubtitle(node)
  const label = [`${name}${shortLabel ? ` ${shortLabel}` : ''}`, look.word, subtitle]
    .filter(Boolean)
    .join(', ')
  const select = () => onSelect(placed.id)
  return (
    <g
      className={`fn s-${tone}${selected ? ' sel' : ''}`}
      data-testid="flow-node"
      data-task-id={placed.id}
      data-node-type={node.node_type ?? 'TASK'}
      data-state={node.state}
      data-container-id={placed.containerId ?? undefined}
      transform={`translate(${placed.x},${placed.y})`}
      tabIndex={0}
      role="button"
      aria-label={label}
      aria-pressed={selected}
      onClick={select}
      onKeyDown={activate(select)}
    >
      <title>{label}</title>
      <desc>{node.state}</desc>
      <rect className="ring" x={-4} y={-4} width={placed.w + 8} height={placed.h + 8} rx={6} />
      <rect
        className="blk"
        width={placed.w}
        height={placed.h}
        rx={3}
        style={tone === 'retry' ? { fill: `url(#${hatch})` } : undefined}
      />
      <IconGlyph name={look.icon} x={12} y={member ? 9 : 10} size={14} />
      {member ? (
        <>
          <text x={34} y={21} className={shortLabel ? 'mono' : undefined}>
            {truncate(shortLabel ?? name, 16)}
          </text>
          <text className="rt" x={placed.w - 10} y={21}>
            {formatSpan(node.runtime_ms)}
          </text>
        </>
      ) : (
        <>
          <text className="t1" x={34} y={21}>
            {truncate(name, 21)}
          </text>
          <text className="t2" x={34} y={38}>
            {truncate(subtitle, 22)}
          </text>
        </>
      )}
    </g>
  )
}

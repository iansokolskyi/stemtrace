/**
 * Task flow graph visualization using React Flow.
 *
 * GROUP and CHORD nodes are rendered as containers with child tasks inside.
 */

import {
  Background,
  BackgroundVariant,
  Controls,
  type Edge,
  type Node,
  type OnNodeDrag,
  Position,
  ReactFlow,
  type ReactFlowInstance,
  useEdgesState,
  useNodesState,
  type Viewport,
  type XYPosition,
} from '@xyflow/react'
import { useEffect, useMemo, useRef, useState } from 'react'
import '@xyflow/react/dist/style.css'
import { Link } from '@tanstack/react-router'
import type { GraphNode, NodeType } from '@/api/client'
import { formatDuration, formatTime } from '@/utils/format'
import { shortestUniqueSuffixes } from '@/utils/taskNames'

interface TaskGraphProps {
  nodes: Record<string, GraphNode>
  rootId: string
}

const stateColors: Record<string, { bg: string; border: string }> = {
  PENDING: { bg: '#374151', border: '#4B5563' },
  RECEIVED: { bg: '#374151', border: '#4B5563' },
  STARTED: { bg: '#1E40AF', border: '#3B82F6' },
  SUCCESS: { bg: '#166534', border: '#22C55E' },
  FAILURE: { bg: '#991B1B', border: '#EF4444' },
  RETRY: { bg: '#92400E', border: '#F59E0B' },
  REVOKED: { bg: '#5B21B6', border: '#8B5CF6' },
  REJECTED: { bg: '#7F1D1D', border: '#DC2626' },
}

const syntheticColors: Record<NodeType, { bg: string; border: string }> = {
  TASK: { bg: '#0F172A', border: '#475569' },
  GROUP: { bg: '#1E1B4B', border: '#6366F1' },
  CHORD: { bg: '#312E81', border: '#818CF8' },
}

const TASK_NODE_MIN_WIDTH = 160
const TASK_NODE_HEIGHT = 100
const CONTAINER_PADDING = 16
const CONTAINER_HEADER = 40
const CHILD_SPACING = 12
const LEVEL_SPACING = 320
const ROW_GAP = 40
const LEVEL_GAP = 160
const CALLBACK_GAP = 60
const FIT_VIEW_OPTIONS = { padding: 0.2 }

/**
 * A node's children without links back to itself. The server never sends
 * those, but a malformed payload must not make a container its own member.
 */
function childIdsOf(node: GraphNode, id: string): string[] {
  return node.children.filter((childId) => childId !== id)
}

/**
 * The row a container draws for one of its children: the child, then down a
 * chain of single children until the group's member (the server puts a
 * group member's chain links inside the group, e.g. group(chain(a, b), ...)).
 */
function containerRow(
  nodes: Record<string, GraphNode>,
  headId: string,
  groupId: string | null,
): string[] {
  const row = [headId]
  let current = nodes[headId]
  while (current && current.group_id !== groupId) {
    const next = childIdsOf(current, row[row.length - 1])
    const nextNode = next.length === 1 ? nodes[next[0]] : undefined
    // Only tasks continue a row: a group or chord child is drawn as its own
    // container next to the row, never inside it as well
    if (!nextNode || (nextNode.node_type ?? 'TASK') !== 'TASK' || row.includes(next[0])) break
    row.push(next[0])
    current = nextNode
  }
  return row
}

/** The longest row length (at least 1), without spreading large arrays. */
function longestRow(rows: string[][]): number {
  return rows.reduce((longest, row) => Math.max(longest, row.length), 1)
}

/**
 * Children of a container's row cells that aren't the next cell of their row
 * (e.g. a header's own subtasks): drawn outside the container, one level on.
 */
function cellChildren(
  nodes: Record<string, GraphNode>,
  rows: string[][],
  containerChildren: Set<string>,
): Array<[string, string]> {
  const pairs: Array<[string, string]> = []
  for (const row of rows) {
    row.forEach((cellId, column) => {
      const cell = nodes[cellId]
      if (!cell) return
      for (const childId of childIdsOf(cell, cellId)) {
        if (childId !== row[column + 1] && !containerChildren.has(childId)) {
          pairs.push([cellId, childId])
        }
      }
    })
  }
  return pairs
}

function buildGraphElements(
  nodes: Record<string, GraphNode>,
  rootId: string,
): { nodes: Node[]; edges: Edge[] } {
  const flowNodes: Node[] = []
  const flowEdges: Edge[] = []
  // GROUP and CHORD nodes are drawn by type, not by name
  const shortNames = shortestUniqueSuffixes(
    Object.values(nodes)
      .filter((node) => (node.node_type ?? 'TASK') === 'TASK')
      .map((node) => node.name),
  )
  const shortName = (name: string): string => shortNames.get(name) ?? name
  const visited = new Set<string>()
  const levels: Map<string, number> = new Map()
  const positions: Map<number, number> = new Map()

  // Track which nodes are inside containers (GROUP/CHORD header tasks)
  // For CHORD, callbacks are NOT inside - they connect via edge
  const containerChildren = new Set<string>()
  const chordCallbacks = new Set<string>()

  for (const [nodeId, node] of Object.entries(nodes)) {
    const nodeType = node.node_type ?? 'TASK'
    if (nodeType === 'GROUP' || nodeType === 'CHORD') {
      // Members (with their chain rows) go inside; a CHORD's callback, named
      // in chord_id, stays outside and connects via edges
      for (const childId of childIdsOf(node, nodeId)) {
        if (nodeType === 'CHORD' && childId === node.chord_id) {
          chordCallbacks.add(childId)
        } else {
          for (const rowId of containerRow(nodes, childId, node.group_id)) {
            containerChildren.add(rowId)
          }
        }
      }
    }
  }
  // The root is drawn even if a malformed payload lists it inside a container
  containerChildren.delete(rootId)

  // BFS to assign levels (skip container children - they're positioned inside)
  const queue: Array<{ id: string; level: number }> = [{ id: rootId, level: 0 }]

  while (queue.length > 0) {
    const { id, level } = queue.shift()!
    if (visited.has(id)) continue
    visited.add(id)

    const node = nodes[id]
    if (!node) continue

    levels.set(id, level)

    const nodeType = node.node_type ?? 'TASK'
    const isContainer = nodeType === 'GROUP' || nodeType === 'CHORD'

    for (const childId of childIdsOf(node, id)) {
      if (!visited.has(childId)) {
        // Container children don't advance the level (they're inside)
        if (isContainer && containerChildren.has(childId)) {
          // Skip - will be positioned inside container
        } else {
          queue.push({ id: childId, level: level + 1 })
        }
      }
    }
    if (isContainer) {
      const heads = childIdsOf(node, id).filter((cid) => !chordCallbacks.has(cid))
      const rows = heads.map((cid) => containerRow(nodes, cid, node.group_id))
      for (const [, childId] of cellChildren(nodes, rows, containerChildren)) {
        if (!visited.has(childId)) queue.push({ id: childId, level: level + 1 })
      }
    }
  }

  // Each level starts right of the widest node of the level before it
  // (a container with chain rows can be wider than a single task)
  const levelWidths: number[] = []
  for (const [id, level] of levels) {
    const node = nodes[id]
    const nodeType = node?.node_type ?? 'TASK'
    let width = TASK_NODE_MIN_WIDTH
    if (node && (nodeType === 'GROUP' || nodeType === 'CHORD')) {
      const heads = childIdsOf(node, id).filter((cid) => !chordCallbacks.has(cid))
      const rowLength = longestRow(heads.map((cid) => containerRow(nodes, cid, node.group_id)))
      width = rowLength * TASK_NODE_MIN_WIDTH + (rowLength - 1) * ROW_GAP + CONTAINER_PADDING * 2
      // A chord's task callback is drawn to the right of its container
      const callback = node.chord_id ? nodes[node.chord_id] : undefined
      if (nodeType === 'CHORD' && callback && (callback.node_type ?? 'TASK') === 'TASK') {
        width += CALLBACK_GAP + TASK_NODE_MIN_WIDTH
      }
    }
    levelWidths[level] = Math.max(levelWidths[level] ?? 0, width)
  }
  const levelStarts: number[] = [0]
  for (let l = 1; l < levelWidths.length; l++) {
    levelStarts[l] =
      levelStarts[l - 1] + Math.max(LEVEL_SPACING, (levelWidths[l - 1] ?? 0) + LEVEL_GAP)
  }
  const levelX = (level: number): number => levelStarts[level] ?? 0

  // Create flow nodes
  visited.clear()
  const queue2: string[] = [rootId]

  while (queue2.length > 0) {
    const id = queue2.shift()!
    if (visited.has(id)) continue
    visited.add(id)

    const node = nodes[id]
    if (!node) continue

    // Skip if this node is inside a container (handled separately)
    if (containerChildren.has(id)) continue

    const level = levels.get(id) ?? 0
    const nodeType = node.node_type ?? 'TASK'
    const isContainer = nodeType === 'GROUP' || nodeType === 'CHORD'
    const stateColor = stateColors[node.state] ?? stateColors.PENDING
    const syntheticColor = syntheticColors[nodeType]

    if (isContainer) {
      // For CHORD, only header tasks go inside (not callbacks)
      const headerChildren = childIdsOf(node, id).filter((cid) => !chordCallbacks.has(cid))
      const rows = headerChildren.map((cid) => containerRow(nodes, cid, node.group_id))
      const childCount = rows.length
      const rowLength = longestRow(rows)

      // Rows stacked in a column; a chain runs left to right within its row
      const containerWidth =
        rowLength * TASK_NODE_MIN_WIDTH + (rowLength - 1) * ROW_GAP + CONTAINER_PADDING * 2
      const containerHeight =
        CONTAINER_HEADER +
        CONTAINER_PADDING * 2 +
        Math.max(1, childCount) * TASK_NODE_HEIGHT +
        Math.max(0, childCount - 1) * CHILD_SPACING

      const yPos = positions.get(level) ?? 0
      positions.set(level, yPos + containerHeight + 40)

      // Container node (parent) - use 'default' type to enable edge handles
      // React Flow's 'group' type doesn't render connection handles
      flowNodes.push({
        id,
        type: 'default',
        position: { x: levelX(level), y: yPos },
        data: { label: '' },
        style: {
          width: containerWidth,
          height: containerHeight,
          backgroundColor: syntheticColor.bg,
          border: `2px dashed ${syntheticColor.border}`,
          borderRadius: '16px',
          padding: 0,
        },
        sourcePosition: Position.Right,
        targetPosition: Position.Left,
        className: 'container-node',
      })

      // Header label inside container (no handles/connectors)
      flowNodes.push({
        id: `${id}-header`,
        type: 'default',
        position: { x: CONTAINER_PADDING, y: 8 },
        parentId: id,
        extent: 'parent',
        draggable: false,
        selectable: false,
        connectable: false,
        data: {
          label: (
            <div className="flex items-center gap-3">
              <span className="text-xs font-mono text-indigo-300 uppercase">{nodeType}</span>
              <span
                className="text-xs px-1.5 py-0.5 rounded"
                style={{ backgroundColor: stateColor.bg, color: stateColor.border }}
              >
                {node.state}
              </span>
            </div>
          ),
        },
        className: 'header-label-node',
        style: {
          background: 'transparent',
          border: 'none',
          padding: 0,
          width: 'auto',
          pointerEvents: 'none' as const,
        },
      })

      // Task nodes inside the container: one row per member
      const rowCells = rows.flatMap((row, idx) =>
        row.map((childId, column) => ({ childId, idx, column, previous: row[column - 1] })),
      )
      rowCells.forEach(({ childId, idx, column, previous }) => {
        const childNode = nodes[childId]
        if (!childNode) return
        if (previous) {
          flowEdges.push({
            id: `${previous}-${childId}`,
            source: previous,
            target: childId,
            animated: childNode.state === 'STARTED',
            style: { stroke: '#475569' },
          })
        }

        const childStateColor = stateColors[childNode.state] ?? stateColors.PENDING
        const duration = formatDuration(childNode.duration_ms)
        const startTime = formatTime(childNode.first_seen)

        flowNodes.push({
          id: childId,
          position: {
            x: CONTAINER_PADDING + column * (TASK_NODE_MIN_WIDTH + ROW_GAP),
            y: CONTAINER_HEADER + CONTAINER_PADDING + idx * (TASK_NODE_HEIGHT + CHILD_SPACING),
          },
          parentId: id,
          extent: 'parent',
          data: {
            label: (
              <Link
                to="/tasks/$taskId"
                params={{ taskId: childId }}
                className="block p-2 text-left h-full"
              >
                <div className="text-xs text-slate-400 font-mono mb-1">{childId.slice(0, 8)}</div>
                <div className="text-sm text-slate-100 font-medium" title={childNode.name}>
                  {shortName(childNode.name)}
                </div>
                {(startTime || duration) && (
                  <div className="text-xs text-slate-500 mt-1">
                    {startTime && <span>{startTime}</span>}
                    {startTime && duration && <span className="mx-1">·</span>}
                    {duration && <span>{duration}</span>}
                  </div>
                )}
                <div
                  className="text-xs mt-1 px-1.5 py-0.5 rounded inline-block"
                  style={{ backgroundColor: childStateColor.bg, color: childStateColor.border }}
                >
                  {childNode.state}
                </div>
              </Link>
            ),
          },
          style: {
            minWidth: TASK_NODE_MIN_WIDTH,
            width: 'fit-content',
            height: TASK_NODE_HEIGHT,
            backgroundColor: '#0F172A',
            border: `2px solid ${childStateColor.border}`,
            borderRadius: '8px',
            padding: 0,
          },
          sourcePosition: Position.Right,
          targetPosition: Position.Left,
          className: 'container-child',
        })

        visited.add(childId)
      })

      // Create callback nodes to the RIGHT of the container (for CHORD)
      if (nodeType === 'CHORD') {
        for (const childId of childIdsOf(node, id)) {
          if (chordCallbacks.has(childId)) {
            const callbackNode = nodes[childId]
            if (!callbackNode) continue

            // Edges from ALL header tasks (each row's member) to the callback:
            // each header's completion contributes to triggering it
            for (const headerId of rows.map((row) => row[row.length - 1])) {
              flowEdges.push({
                id: `${id}-callback-${headerId}-${childId}`,
                source: headerId,
                target: childId,
                animated: callbackNode.state === 'STARTED',
                style: { stroke: '#475569' },
              })
            }

            // A body that is itself a group or chord is drawn as a container
            // at the next level, like any child; only its edges are added here
            const callbackType = callbackNode.node_type ?? 'TASK'
            if (callbackType === 'GROUP' || callbackType === 'CHORD') continue

            const callbackStateColor = stateColors[callbackNode.state] ?? stateColors.PENDING
            const callbackDuration = formatDuration(callbackNode.duration_ms)
            const callbackStartTime = formatTime(callbackNode.first_seen)

            // Position callback to the RIGHT of container, vertically centered
            const callbackX = levelX(level) + containerWidth + CALLBACK_GAP
            const callbackY = yPos + (containerHeight - TASK_NODE_HEIGHT) / 2

            flowNodes.push({
              id: childId,
              position: { x: callbackX, y: callbackY },
              data: {
                label: (
                  <Link
                    to="/tasks/$taskId"
                    params={{ taskId: childId }}
                    className="block p-2 text-left h-full"
                  >
                    <div className="text-xs text-slate-400 font-mono mb-1">
                      {childId.slice(0, 8)}
                    </div>
                    <div className="text-sm text-slate-100 font-medium" title={callbackNode.name}>
                      {shortName(callbackNode.name)}
                    </div>
                    {(callbackStartTime || callbackDuration) && (
                      <div className="text-xs text-slate-500 mt-1">
                        {callbackStartTime && <span>{callbackStartTime}</span>}
                        {callbackStartTime && callbackDuration && <span className="mx-1">·</span>}
                        {callbackDuration && <span>{callbackDuration}</span>}
                      </div>
                    )}
                    <div
                      className="text-xs mt-1 px-1.5 py-0.5 rounded inline-block"
                      style={{
                        backgroundColor: callbackStateColor.bg,
                        color: callbackStateColor.border,
                      }}
                    >
                      {callbackNode.state}
                    </div>
                  </Link>
                ),
              },
              style: {
                minWidth: TASK_NODE_MIN_WIDTH,
                width: 'fit-content',
                height: TASK_NODE_HEIGHT,
                backgroundColor: '#0F172A',
                border: `2px solid ${callbackStateColor.border}`,
                borderRadius: '8px',
                padding: 0,
              },
              sourcePosition: Position.Right,
              targetPosition: Position.Left,
            })

            visited.add(childId)
          }
        }
      }
    } else {
      // Regular task node
      const yPos = positions.get(level) ?? 0
      positions.set(level, yPos + 120)

      const duration = formatDuration(node.duration_ms)
      const startTime = formatTime(node.first_seen)

      flowNodes.push({
        id,
        position: { x: levelX(level), y: yPos },
        data: {
          label: (
            <Link to="/tasks/$taskId" params={{ taskId: id }} className="block p-3 text-left">
              <div className="text-xs text-slate-400 font-mono mb-1">{id.slice(0, 8)}</div>
              <div
                className="text-sm text-slate-100 font-medium whitespace-nowrap"
                title={node.name}
              >
                {shortName(node.name)}
              </div>
              {(startTime || duration) && (
                <div className="text-xs text-slate-500 mt-1">
                  {startTime && <span>{startTime}</span>}
                  {startTime && duration && <span className="mx-1">·</span>}
                  {duration && <span>{duration}</span>}
                </div>
              )}
              <div
                className="text-xs mt-2 px-1.5 py-0.5 rounded inline-block"
                style={{ backgroundColor: stateColor.bg, color: stateColor.border }}
              >
                {node.state}
              </div>
            </Link>
          ),
        },
        style: {
          padding: 0,
          borderRadius: '8px',
          border: `2px solid ${stateColor.border}`,
          backgroundColor: '#0F172A',
          minWidth: TASK_NODE_MIN_WIDTH,
          width: 'fit-content',
        },
        sourcePosition: Position.Right,
        targetPosition: Position.Left,
      })

      // Edges to children (not inside a container)
      for (const childId of childIdsOf(node, id)) {
        // Skip edges to nodes that are inside containers
        if (containerChildren.has(childId)) continue

        const childNode = nodes[childId]
        flowEdges.push({
          id: `${id}-${childId}`,
          source: id,
          target: childId,
          animated: childNode?.state === 'STARTED',
          style: { stroke: '#475569' },
        })
      }
    }

    // Queue non-container children
    for (const childId of childIdsOf(node, id)) {
      if (!visited.has(childId) && !containerChildren.has(childId)) {
        queue2.push(childId)
      }
    }
    // A container's cells can have children of their own (a header's
    // subtasks): edge from the cell, drawn outside like any child
    if (isContainer) {
      const heads = childIdsOf(node, id).filter((cid) => !chordCallbacks.has(cid))
      const rows = heads.map((cid) => containerRow(nodes, cid, node.group_id))
      for (const [cellId, childId] of cellChildren(nodes, rows, containerChildren)) {
        const childNode = nodes[childId]
        if (!childNode) continue
        flowEdges.push({
          id: `${cellId}-${childId}`,
          source: cellId,
          target: childId,
          animated: childNode.state === 'STARTED',
          style: { stroke: '#475569' },
        })
        if (!visited.has(childId)) queue2.push(childId)
      }
    }
  }

  return { nodes: flowNodes, edges: flowEdges }
}

function sameViewport(a: Viewport, b: Viewport): boolean {
  return a.x === b.x && a.y === b.y && a.zoom === b.zoom
}

/**
 * The graph for `graphNodes`, kept in step with it: new tasks, new links and
 * state changes show up as the data changes (a WebSocket event refetches the
 * graph, or polling does when the socket is down).
 *
 * React Flow's `useNodesState(initial)` reads `initial` once, so the laid-out
 * elements are copied into its state whenever they change. Two things the
 * user did are kept across that copy: where they dragged a node, and the
 * viewport once they have panned or zoomed (until then, the view re-fits when
 * the set of nodes changes, so a new task is never off screen).
 */
export function TaskGraph({ nodes: graphNodes, rootId }: TaskGraphProps) {
  const layout = useMemo(() => buildGraphElements(graphNodes, rootId), [graphNodes, rootId])

  const [nodes, setNodes, onNodesChange] = useNodesState(layout.nodes)
  const [edges, setEdges, onEdgesChange] = useEdgesState(layout.edges)
  const flow = useRef<ReactFlowInstance | null>(null)
  const draggedPositions = useRef(new Map<string, XYPosition>())
  const viewportChosen = useRef(false)
  // Where the viewport was when the current user pan or zoom began
  const moveStart = useRef<Viewport | null>(null)
  const shownNodeIds = useRef(layout.nodes.map((node) => node.id).join())
  // The ids a re-fit is waiting for React Flow to hold (see the second effect)
  const fitWhenHolding = useRef<string | null>(null)
  // A node is held by the mouse: a re-fit would move the view under it, so it
  // waits for the drop
  const [dragging, setDragging] = useState(false)

  useEffect(() => {
    setNodes((previous) => {
      const shown = new Map(previous.map((node) => [node.id, node]))
      return layout.nodes.map((node) => {
        const was = shown.get(node.id)
        return {
          ...node,
          // Keep the size React Flow measured: without it the node is hidden
          // until it is measured again
          measured: was?.measured,
          // A node under the mouse stays where the mouse has it; the dragged
          // position is only recorded when the drop ends
          dragging: was?.dragging,
          position: was?.dragging
            ? was.position
            : (draggedPositions.current.get(node.id) ?? node.position),
        }
      })
    })
    setEdges(layout.edges)

    const nodeIds = layout.nodes.map((node) => node.id).join()
    if (nodeIds !== shownNodeIds.current) {
      shownNodeIds.current = nodeIds
      if (!viewportChosen.current) fitWhenHolding.current = nodeIds
    }
  }, [layout, setNodes, setEdges])

  // Fit once React Flow holds the new nodes: asked any earlier (in the effect
  // above, where setNodes has only been queued) fitView fits the old ones.
  useEffect(() => {
    if (fitWhenHolding.current === null || dragging) return
    if (nodes.map((node) => node.id).join() !== fitWhenHolding.current) return
    fitWhenHolding.current = null
    void flow.current?.fitView(FIT_VIEW_OPTIONS)
  }, [nodes, dragging])

  const rememberDraggedPositions: OnNodeDrag = (_event, _node, draggedNodes) => {
    for (const node of draggedNodes) draggedPositions.current.set(node.id, node.position)
    setDragging(false)
  }

  if (Object.keys(graphNodes).length === 0) {
    return (
      <div className="flex items-center justify-center h-full text-slate-400">
        No nodes in graph
      </div>
    )
  }

  return (
    <ReactFlow
      nodes={nodes}
      edges={edges}
      onNodesChange={onNodesChange}
      onEdgesChange={onEdgesChange}
      onInit={(instance) => {
        flow.current = instance
      }}
      onNodeDragStart={() => setDragging(true)}
      onNodeDragStop={rememberDraggedPositions}
      // A pan or zoom the user made comes with its mouse or touch event; the fits
      // the page does itself (and the Controls buttons) come without one. A
      // mouse down on the canvas starts a move too, so what counts is whether the
      // viewport is somewhere else when it ends: a plain click moves nothing.
      onMoveStart={(event, viewport) => {
        moveStart.current = event ? viewport : null
      }}
      onMoveEnd={(event, viewport) => {
        const start = moveStart.current
        moveStart.current = null
        if (event && start && !sameViewport(start, viewport)) viewportChosen.current = true
      }}
      fitView
      fitViewOptions={FIT_VIEW_OPTIONS}
      minZoom={0.1}
      maxZoom={2}
      proOptions={{ hideAttribution: true }}
    >
      <Controls
        className="bg-slate-800 border-slate-700"
        onZoomIn={() => {
          viewportChosen.current = true
        }}
        onZoomOut={() => {
          viewportChosen.current = true
        }}
        onFitView={() => {
          viewportChosen.current = false
        }}
      />
      <Background variant={BackgroundVariant.Dots} gap={16} size={1} color="#1E293B" />
    </ReactFlow>
  )
}

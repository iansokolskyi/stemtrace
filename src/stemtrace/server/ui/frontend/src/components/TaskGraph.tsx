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
  Position,
  ReactFlow,
  useEdgesState,
  useNodesState,
} from '@xyflow/react'
import { useMemo } from 'react'
import '@xyflow/react/dist/style.css'
import { Link } from '@tanstack/react-router'
import type { GraphNode, NodeType } from '@/api/client'
import { formatDuration, formatTime } from '@/utils/format'

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
    if (next.length !== 1 || row.includes(next[0]) || !nodes[next[0]]) break
    row.push(next[0])
    current = nodes[next[0]]
  }
  return row
}

function buildGraphElements(
  nodes: Record<string, GraphNode>,
  rootId: string,
): { nodes: Node[]; edges: Edge[] } {
  const flowNodes: Node[] = []
  const flowEdges: Edge[] = []
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
      const rowLength = Math.max(
        1,
        ...heads.map((cid) => containerRow(nodes, cid, node.group_id).length),
      )
      width = rowLength * TASK_NODE_MIN_WIDTH + (rowLength - 1) * ROW_GAP + CONTAINER_PADDING * 2
    }
    levelWidths[level] = Math.max(levelWidths[level] ?? 0, width)
  }
  const levelX = (level: number): number => {
    let x = 0
    for (let l = 0; l < level; l++) {
      x += Math.max(LEVEL_SPACING, (levelWidths[l] ?? 0) + LEVEL_GAP)
    }
    return x
  }

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
      const rowLength = Math.max(1, ...rows.map((row) => row.length))

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
                <div className="text-sm text-slate-100 font-medium">
                  {childNode.name.split('.').pop()}
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
            const callbackX = levelX(level) + containerWidth + 60
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
                    <div className="text-sm text-slate-100 font-medium">
                      {callbackNode.name.split('.').pop()}
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
              <div className="text-sm text-slate-100 font-medium whitespace-nowrap">
                {node.name.split('.').pop()}
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
  }

  return { nodes: flowNodes, edges: flowEdges }
}

export function TaskGraph({ nodes: graphNodes, rootId }: TaskGraphProps) {
  const { nodes: initialNodes, edges: initialEdges } = useMemo(
    () => buildGraphElements(graphNodes, rootId),
    [graphNodes, rootId],
  )

  const [nodes, , onNodesChange] = useNodesState(initialNodes)
  const [edges, , onEdgesChange] = useEdgesState(initialEdges)

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
      fitView
      fitViewOptions={{ padding: 0.2 }}
      minZoom={0.1}
      maxZoom={2}
      proOptions={{ hideAttribution: true }}
    >
      <Controls className="bg-slate-800 border-slate-700" />
      <Background variant={BackgroundVariant.Dots} gap={16} size={1} color="#1E293B" />
    </ReactFlow>
  )
}

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

  for (const node of Object.values(nodes)) {
    const nodeType = node.node_type ?? 'TASK'
    if (nodeType === 'GROUP') {
      for (const childId of node.children) {
        containerChildren.add(childId)
      }
    } else if (nodeType === 'CHORD') {
      // For CHORD, header tasks go inside, callback stays outside
      // The CHORD node's chord_id field contains the callback task_id
      for (const childId of node.children) {
        if (childId === node.chord_id) {
          // This is the callback - render outside with edge
          chordCallbacks.add(childId)
        } else {
          // Header task - render inside container
          containerChildren.add(childId)
        }
      }
    }
  }

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

    for (const childId of node.children) {
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
      const headerChildren = node.children.filter((cid) => !chordCallbacks.has(cid))
      const childCount = headerChildren.length

      // Vertical layout: children stacked in a column
      const containerWidth = TASK_NODE_MIN_WIDTH + CONTAINER_PADDING * 2
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
        position: { x: level * LEVEL_SPACING, y: yPos },
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

      // Header task nodes inside container (vertical column layout)
      headerChildren.forEach((childId, idx) => {
        const childNode = nodes[childId]
        if (!childNode) return

        const childStateColor = stateColors[childNode.state] ?? stateColors.PENDING
        const duration = formatDuration(childNode.duration_ms)
        const startTime = formatTime(childNode.first_seen)

        flowNodes.push({
          id: childId,
          position: {
            x: CONTAINER_PADDING,
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
        for (const childId of node.children) {
          if (chordCallbacks.has(childId)) {
            const callbackNode = nodes[childId]
            if (!callbackNode) continue

            const callbackStateColor = stateColors[callbackNode.state] ?? stateColors.PENDING
            const callbackDuration = formatDuration(callbackNode.duration_ms)
            const callbackStartTime = formatTime(callbackNode.first_seen)

            // Position callback to the RIGHT of container, vertically centered
            const callbackX = level * LEVEL_SPACING + containerWidth + 60
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

            // Edges from ALL header tasks to callback (chord aggregation)
            // Each header task's completion contributes to triggering the callback
            for (const headerId of headerChildren) {
              flowEdges.push({
                id: `${id}-callback-${headerId}-${childId}`,
                source: headerId,
                target: childId,
                animated: callbackNode.state === 'STARTED',
                style: { stroke: '#475569' },
              })
            }

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
        position: { x: level * LEVEL_SPACING, y: yPos },
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
      for (const childId of node.children) {
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
    for (const childId of node.children) {
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

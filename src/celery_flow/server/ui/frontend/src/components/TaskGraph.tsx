/**
 * Task flow graph visualization using React Flow.
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
import { useCallback, useMemo } from 'react'
import '@xyflow/react/dist/style.css'
import { Link } from '@tanstack/react-router'
import type { GraphNode } from '@/api/client'

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

function buildGraphElements(
  nodes: Record<string, GraphNode>,
  rootId: string,
): { nodes: Node[]; edges: Edge[] } {
  const flowNodes: Node[] = []
  const flowEdges: Edge[] = []
  const visited = new Set<string>()
  const levels: Map<string, number> = new Map()
  const positions: Map<number, number> = new Map() // level -> y position

  // BFS to assign levels
  const queue: Array<{ id: string; level: number }> = [{ id: rootId, level: 0 }]

  while (queue.length > 0) {
    const { id, level } = queue.shift()!
    if (visited.has(id)) continue
    visited.add(id)

    const node = nodes[id]
    if (!node) continue

    levels.set(id, level)

    for (const childId of node.children) {
      if (!visited.has(childId)) {
        queue.push({ id: childId, level: level + 1 })
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

    const level = levels.get(id) ?? 0
    const yPos = positions.get(level) ?? 0
    positions.set(level, yPos + 100)

    const colors = stateColors[node.state] ?? stateColors.PENDING

    flowNodes.push({
      id,
      position: { x: level * 250, y: yPos },
      data: {
        label: (
          <Link to="/tasks/$taskId" params={{ taskId: id }} className="block p-3 text-left">
            <div className="text-xs text-slate-400 font-mono mb-1">{id.slice(0, 8)}</div>
            <div className="text-sm text-slate-100 font-medium truncate max-w-[150px]">
              {node.name.split('.').pop()}
            </div>
            <div
              className="text-xs mt-2 px-1.5 py-0.5 rounded inline-block"
              style={{
                backgroundColor: colors.bg,
                color: colors.border,
              }}
            >
              {node.state}
            </div>
          </Link>
        ),
      },
      style: {
        padding: 0,
        borderRadius: '8px',
        border: `2px solid ${colors.border}`,
        backgroundColor: '#0F172A',
        width: 180,
      },
      sourcePosition: Position.Right,
      targetPosition: Position.Left,
    })

    // Create edges
    for (const childId of node.children) {
      flowEdges.push({
        id: `${id}-${childId}`,
        source: id,
        target: childId,
        animated: nodes[childId]?.state === 'STARTED',
        style: { stroke: '#475569' },
      })

      if (!visited.has(childId)) {
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

  const onInit = useCallback(() => {
    // Could fit view here if needed
  }, [])

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
      onInit={onInit}
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

import clsx from 'clsx'
import { useRef, useState } from 'react'
import type { TaskEvent } from '../api/client'

const stateColors: Record<string, string> = {
  PENDING: 'bg-slate-500',
  RECEIVED: 'bg-slate-400',
  STARTED: 'bg-blue-500',
  SUCCESS: 'bg-emerald-500',
  FAILURE: 'bg-red-500',
  RETRY: 'bg-amber-500',
  REVOKED: 'bg-purple-500',
  REJECTED: 'bg-red-600',
}

const TERMINAL_STATES = new Set(['SUCCESS', 'FAILURE', 'REVOKED', 'REJECTED'])

// State priority within a retry cycle: PENDING → RECEIVED → STARTED → RETRY → terminal
const STATE_PRIORITY: Record<string, number> = {
  PENDING: 0,
  RECEIVED: 1,
  STARTED: 2,
  RETRY: 3,
  SUCCESS: 10,
  FAILURE: 10,
  REVOKED: 10,
  REJECTED: 10,
}

const heightClasses = { sm: 'h-6', md: 'h-8', lg: 'h-10' }

const terminalIcons: Record<string, string> = {
  SUCCESS: '✓',
  FAILURE: '×',
  REVOKED: '⊘',
  REJECTED: '⊘',
}

interface TimelineSegment {
  state: string
  duration: number
  timestamp: Date
  isTerminal: boolean
  widthPct: number
}

function buildSegments(events: TaskEvent[]): TimelineSegment[] {
  if (!events || events.length === 0) return []

  // Filter: only keep first PENDING and first RECEIVED (retry re-queues create duplicates)
  let hasPending = false
  let hasReceived = false
  const filtered = events.filter((e) => {
    if (e.state === 'PENDING') {
      if (hasPending) return false
      hasPending = true
    }
    if (e.state === 'RECEIVED') {
      if (hasReceived) return false
      hasReceived = true
    }
    return true
  })

  // Sort by logical order: (retry_count, state_priority, timestamp)
  // This ensures correct ordering regardless of timing issues
  const sorted = filtered.sort((a, b) => {
    const retryA = a.retries ?? 0
    const retryB = b.retries ?? 0

    // 1. Sort by retry count first
    if (retryA !== retryB) return retryA - retryB

    // 2. Within same retry, sort by state lifecycle
    const priorityA = STATE_PRIORITY[a.state] ?? 5
    const priorityB = STATE_PRIORITY[b.state] ?? 5
    if (priorityA !== priorityB) return priorityA - priorityB

    // 3. Same retry and state: use timestamp as tiebreaker
    return new Date(a.timestamp).getTime() - new Date(b.timestamp).getTime()
  })

  const minTime = new Date(sorted[0].timestamp).getTime()
  const maxTime = new Date(sorted[sorted.length - 1].timestamp).getTime()
  const range = maxTime - minTime || 1

  const segments: TimelineSegment[] = []

  for (let i = 0; i < sorted.length; i++) {
    const event = sorted[i]
    const eventTime = new Date(event.timestamp).getTime()
    const isTerminal = TERMINAL_STATES.has(event.state)
    const nextEvent = sorted[i + 1]
    const endTime = nextEvent ? new Date(nextEvent.timestamp).getTime() : eventTime

    const duration = endTime - eventTime
    const widthPct = range > 0 ? (duration / range) * 100 : 100 / sorted.length

    segments.push({
      state: event.state,
      duration,
      timestamp: new Date(event.timestamp),
      isTerminal,
      widthPct,
    })
  }

  return segments
}

function formatDuration(ms: number): string {
  if (ms < 1000) return `${ms}ms`
  if (ms < 60000) return `${(ms / 1000).toFixed(1)}s`
  return `${(ms / 60000).toFixed(1)}m`
}

function formatTime(date: Date): string {
  return date.toLocaleTimeString()
}

interface TimelineBarProps {
  events: TaskEvent[]
  height?: 'sm' | 'md' | 'lg'
  showLabels?: boolean
}

export function TimelineBar({ events, height = 'sm', showLabels = false }: TimelineBarProps) {
  const [hoveredIndex, setHoveredIndex] = useState<number | null>(null)
  const [mouseX, setMouseX] = useState(0)
  const containerRef = useRef<HTMLDivElement>(null)

  const segments = buildSegments(events)

  if (segments.length === 0) {
    return <div className={heightClasses[height]} />
  }

  const totalDuration = segments.reduce((sum, s) => sum + s.duration, 0) || 1

  return (
    <div
      ref={containerRef}
      className={clsx('relative overflow-visible flex', heightClasses[height])}
      onMouseMove={(e) => {
        if (containerRef.current) {
          const rect = containerRef.current.getBoundingClientRect()
          setMouseX(e.clientX - rect.left)
        }
      }}
    >
      {segments.map((segment, i) => {
        const durationRatio = totalDuration > 0 ? segment.duration / totalDuration : 0
        const isHovered = hoveredIndex === i
        const isFirst = i === 0
        const isLast = i === segments.length - 1

        // Terminal states get fixed width, others grow proportionally
        const flexStyle = segment.isTerminal
          ? '0 0 20px'
          : `${Math.max(1, durationRatio * 100)} 1 ${Math.max(4, durationRatio * 100)}%`

        return (
          <div
            key={i}
            role="presentation"
            className={clsx(
              'h-full flex items-center justify-center cursor-pointer',
              'transition-[filter,transform] duration-75',
              stateColors[segment.state] ?? 'bg-gray-500',
              i > 0 && 'border-l border-slate-900/30',
              isHovered && 'brightness-125 z-10',
              isFirst && 'rounded-l',
              isLast && 'rounded-r',
            )}
            style={{
              flex: flexStyle,
              minWidth: segment.isTerminal ? '20px' : '6px',
              maxWidth: segment.isTerminal ? '20px' : undefined,
            }}
            onMouseEnter={() => setHoveredIndex(i)}
            onMouseLeave={() => setHoveredIndex(null)}
          >
            {showLabels && (
              <span className="text-xs font-semibold text-white/90">
                {segment.isTerminal
                  ? (terminalIcons[segment.state] ?? '⊘')
                  : durationRatio > 0.15
                    ? segment.state
                    : segment.state[0]}
              </span>
            )}
            {!showLabels && segment.isTerminal && (
              <span className="text-xs font-semibold text-white/90">
                {terminalIcons[segment.state] ?? '⊘'}
              </span>
            )}
          </div>
        )
      })}

      {hoveredIndex !== null && segments[hoveredIndex] && (
        <div
          className="absolute -top-12 bg-slate-950 border border-slate-700 text-white text-xs px-3 py-2 rounded-lg shadow-xl whitespace-nowrap z-20 pointer-events-none"
          style={{ left: mouseX, transform: 'translateX(-50%)' }}
        >
          <div className="flex items-center gap-2">
            <div
              className={clsx('w-2 h-2 rounded-sm', stateColors[segments[hoveredIndex].state])}
            />
            <span className="font-medium">{segments[hoveredIndex].state}</span>
            <span className="text-slate-400">•</span>
            <span className="font-mono">{formatDuration(segments[hoveredIndex].duration)}</span>
          </div>
          <div className="text-slate-400 mt-1 font-mono text-[10px]">
            {formatTime(segments[hoveredIndex].timestamp)}
          </div>
        </div>
      )}
    </div>
  )
}

export { stateColors, TERMINAL_STATES, buildSegments, formatDuration, formatTime }
export type { TimelineSegment }

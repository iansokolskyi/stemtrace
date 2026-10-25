/**
 * WebSocket context for sharing connection status globally.
 */

import { useQueryClient } from '@tanstack/react-query'
import {
  createContext,
  type ReactNode,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
} from 'react'
import { isRedirectingToLogin, type TaskDetailResponse, type TaskEvent } from '@/api/client'

type ConnectionStatus = 'connecting' | 'connected' | 'disconnected' | 'error'

interface WebSocketContextValue {
  connectionStatus: ConnectionStatus
  isConnected: boolean
  lastEvent: TaskEvent | null
  events: TaskEvent[]
}

const WebSocketContext = createContext<WebSocketContextValue | null>(null)

// WebSocket URL: use injected base path in production
const getWsUrl = (): string => {
  const base = window.__STEMTRACE_BASE__ ?? '/stemtrace'
  if (import.meta.env.DEV) {
    return `ws://${window.location.hostname}:8000${base}/ws`
  }
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${protocol}//${window.location.host}${base}/ws`
}
const WS_URL = getWsUrl()

/**
 * Whether a live event changes what the task detail page for `taskId` shows:
 * the task itself, a child of it (the page lists children with their state),
 * or, for a synthetic GROUP/CHORD page (`group:<group_id>`), a member (by
 * group_id) or the chord callback (listed among the node's children).
 */
function detailAffectedBy(taskId: string, detail: unknown, event: TaskEvent): boolean {
  if (taskId === event.task_id) return true
  if (event.parent_id && taskId === event.parent_id) return true
  if (event.group_id && taskId === `group:${event.group_id}`) return true
  const children = (detail as TaskDetailResponse | undefined)?.task?.children
  return Array.isArray(children) && children.includes(event.task_id)
}

// Reconnect with exponential backoff: 3s for quick recovery, doubling up to 60s.
// A denied handshake (e.g. expired session) is indistinguishable from the server
// being down, so backing off keeps us from hammering the server forever.
const RECONNECT_BASE_DELAY = 3000
const RECONNECT_MAX_DELAY = 60000

// A round of refetches that has not finished after this long stops holding up
// the next one (a request that never answers must not freeze live updates).
const REFETCH_ROUND_MAX_MS = 15000

interface WebSocketProviderProps {
  children: ReactNode
}

export function WebSocketProvider({ children }: WebSocketProviderProps) {
  const [connectionStatus, setConnectionStatus] = useState<ConnectionStatus>('connecting')
  const [lastEvent, setLastEvent] = useState<TaskEvent | null>(null)
  const [events, setEvents] = useState<TaskEvent[]>([])
  const queryClient = useQueryClient()
  const wsRef = useRef<WebSocket | null>(null)
  const reconnectTimeoutRef = useRef<number | null>(null)
  const reconnectAttemptsRef = useRef(0)
  const pendingEventsRef = useRef<TaskEvent[]>([])
  const refetchingRef = useRef(false)

  // Refetch what the events since the last round touched, one round at a time.
  // Invalidating on every event cancels the fetch already in flight (TanStack
  // Query's default), so when events arrive faster than the API answers every
  // fetch is cancelled and nothing updates until the burst ends. Here events
  // that arrive during a round wait for it to finish and share the next one.
  const refetchForEvents = useCallback(async () => {
    if (refetchingRef.current) return
    refetchingRef.current = true
    try {
      while (pendingEventsRef.current.length > 0) {
        const events = pendingEventsRef.current.splice(0)
        // Invalidate both regular and infinite queries. ['tasks', params] is
        // a list; ['tasks', taskId] is a task detail (with its captured
        // output), refetched only when an event concerns that page.
        const round = Promise.all([
          queryClient.invalidateQueries({
            queryKey: ['tasks'],
            predicate: (query) => {
              const key = query.queryKey[1]
              if (typeof key !== 'string') return true
              return events.some((event) => detailAffectedBy(key, query.state.data, event))
            },
          }),
          queryClient.invalidateQueries({ queryKey: ['executions'] }),
          queryClient.invalidateQueries({ queryKey: ['graphs'] }),
          queryClient.invalidateQueries({ queryKey: ['graphs-infinite'] }),
        ])
        let timer: number | undefined
        const tooLong = new Promise((resolve) => {
          timer = window.setTimeout(resolve, REFETCH_ROUND_MAX_MS)
        })
        await Promise.race([round, tooLong])
        window.clearTimeout(timer)
      }
    } finally {
      refetchingRef.current = false
    }
  }, [queryClient])

  const connect = useCallback(() => {
    // Don't reconnect if already connected or connecting
    if (
      wsRef.current?.readyState === WebSocket.OPEN ||
      wsRef.current?.readyState === WebSocket.CONNECTING
    ) {
      return
    }

    setConnectionStatus('connecting')

    const scheduleReconnect = () => {
      if (reconnectTimeoutRef.current) {
        clearTimeout(reconnectTimeoutRef.current)
      }
      // Page is navigating to the login form; reconnecting would just be denied
      if (isRedirectingToLogin()) return
      const delay = Math.min(
        RECONNECT_BASE_DELAY * 2 ** reconnectAttemptsRef.current,
        RECONNECT_MAX_DELAY,
      )
      reconnectAttemptsRef.current += 1
      console.log(`[WebSocket] Disconnected, will retry in ${delay / 1000}s...`)
      reconnectTimeoutRef.current = window.setTimeout(() => {
        connect()
      }, delay)
    }

    try {
      const ws = new WebSocket(WS_URL)
      wsRef.current = ws

      ws.onopen = () => {
        setConnectionStatus('connected')
        reconnectAttemptsRef.current = 0
        console.log('[WebSocket] Connected')
      }

      ws.onmessage = (event) => {
        try {
          const data = JSON.parse(event.data) as TaskEvent
          setLastEvent(data)
          setEvents((prev) => [data, ...prev].slice(0, 100)) // Keep last 100

          pendingEventsRef.current.push(data)
          void refetchForEvents()
        } catch (e) {
          console.error('[WebSocket] Failed to parse message:', e)
        }
      }

      ws.onclose = () => {
        setConnectionStatus('disconnected')
        wsRef.current = null
        scheduleReconnect()
      }

      ws.onerror = () => {
        setConnectionStatus('error')
        // onclose will be called after onerror, which will trigger reconnect
      }
    } catch (e) {
      setConnectionStatus('error')
      console.error('[WebSocket] Failed to connect:', e)
      scheduleReconnect()
    }
  }, [refetchForEvents])

  useEffect(() => {
    connect()

    return () => {
      if (reconnectTimeoutRef.current) {
        clearTimeout(reconnectTimeoutRef.current)
      }
      const ws = wsRef.current
      wsRef.current = null
      if (ws) {
        // Detach before closing: the close event arrives after StrictMode's
        // second mount has opened a replacement, and the handler would then
        // null that socket's ref, report "disconnected" (turning polling on)
        // and schedule a reconnect that opens a second live socket.
        ws.onopen = null
        ws.onmessage = null
        ws.onerror = null
        ws.onclose = null
        ws.close()
      }
    }
  }, [connect])

  const value: WebSocketContextValue = {
    connectionStatus,
    isConnected: connectionStatus === 'connected',
    lastEvent,
    events,
  }

  return <WebSocketContext.Provider value={value}>{children}</WebSocketContext.Provider>
}

export function useWebSocketContext(): WebSocketContextValue {
  const context = useContext(WebSocketContext)
  if (!context) {
    throw new Error('useWebSocketContext must be used within a WebSocketProvider')
  }
  return context
}

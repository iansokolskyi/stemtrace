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
import { isRedirectingToLogin, type TaskEvent } from '@/api/client'

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

// Reconnect with exponential backoff: 3s for quick recovery, doubling up to 60s.
// A denied handshake (e.g. expired session) is indistinguishable from the server
// being down, so backing off keeps us from hammering the server forever.
const RECONNECT_BASE_DELAY = 3000
const RECONNECT_MAX_DELAY = 60000

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

          // Invalidate queries to refresh data
          // Invalidate both regular and infinite queries. ['tasks', params] is
          // a list; ['tasks', taskId] is a task detail (with its captured
          // output), refetched only for an event of that task or of one of
          // its children (the detail page lists children with their state).
          queryClient.invalidateQueries({
            queryKey: ['tasks'],
            predicate: (query) => {
              const key = query.queryKey[1]
              if (typeof key !== 'string') return true
              return key === data.task_id || (!!data.parent_id && key === data.parent_id)
            },
          })
          queryClient.invalidateQueries({ queryKey: ['tasks-infinite'] })
          queryClient.invalidateQueries({ queryKey: ['graphs'] })
          queryClient.invalidateQueries({ queryKey: ['graphs-infinite'] })
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
  }, [queryClient])

  useEffect(() => {
    connect()

    return () => {
      if (reconnectTimeoutRef.current) {
        clearTimeout(reconnectTimeoutRef.current)
      }
      if (wsRef.current) {
        wsRef.current.close()
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

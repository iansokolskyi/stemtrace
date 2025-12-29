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
import type { TaskEvent } from '@/api/client'

type ConnectionStatus = 'connecting' | 'connected' | 'disconnected' | 'error'

interface WebSocketContextValue {
  connectionStatus: ConnectionStatus
  isConnected: boolean
  lastEvent: TaskEvent | null
  events: TaskEvent[]
}

const WebSocketContext = createContext<WebSocketContextValue | null>(null)

// WebSocket URL
const WS_URL = import.meta.env.DEV
  ? `ws://${window.location.hostname}:8000/stemtrace/ws`
  : `${window.location.protocol === 'https:' ? 'wss:' : 'ws:'}//${window.location.host}/stemtrace/ws`

// Reconnect interval when disconnected (3 seconds for quick recovery)
const RECONNECT_INTERVAL = 3000

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

  const connect = useCallback(() => {
    // Don't reconnect if already connected or connecting
    if (
      wsRef.current?.readyState === WebSocket.OPEN ||
      wsRef.current?.readyState === WebSocket.CONNECTING
    ) {
      return
    }

    setConnectionStatus('connecting')

    try {
      const ws = new WebSocket(WS_URL)
      wsRef.current = ws

      ws.onopen = () => {
        setConnectionStatus('connected')
        console.log('[WebSocket] Connected')
      }

      ws.onmessage = (event) => {
        try {
          const data = JSON.parse(event.data) as TaskEvent
          setLastEvent(data)
          setEvents((prev) => [data, ...prev].slice(0, 100)) // Keep last 100

          // Invalidate queries to refresh data
          queryClient.invalidateQueries({ queryKey: ['tasks'] })
          queryClient.invalidateQueries({ queryKey: ['graphs'] })
        } catch (e) {
          console.error('[WebSocket] Failed to parse message:', e)
        }
      }

      ws.onclose = () => {
        setConnectionStatus('disconnected')
        wsRef.current = null
        console.log('[WebSocket] Disconnected, will retry in 3s...')

        // Schedule reconnect
        if (reconnectTimeoutRef.current) {
          clearTimeout(reconnectTimeoutRef.current)
        }
        reconnectTimeoutRef.current = window.setTimeout(() => {
          connect()
        }, RECONNECT_INTERVAL)
      }

      ws.onerror = () => {
        setConnectionStatus('error')
        // onclose will be called after onerror, which will trigger reconnect
      }
    } catch (e) {
      setConnectionStatus('error')
      console.error('[WebSocket] Failed to connect:', e)

      // Schedule reconnect on error
      if (reconnectTimeoutRef.current) {
        clearTimeout(reconnectTimeoutRef.current)
      }
      reconnectTimeoutRef.current = window.setTimeout(() => {
        connect()
      }, RECONNECT_INTERVAL)
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

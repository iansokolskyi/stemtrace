/**
 * WebSocket hook for real-time task updates.
 */

import { useQueryClient } from '@tanstack/react-query'
import { useCallback, useEffect, useRef, useState } from 'react'
import type { TaskEvent } from '@/api/client'

type ConnectionStatus = 'connecting' | 'connected' | 'disconnected' | 'error'

interface UseWebSocketReturn {
  connectionStatus: ConnectionStatus
  lastEvent: TaskEvent | null
  events: TaskEvent[]
}

// WebSocket URL
const WS_URL = import.meta.env.DEV
  ? `ws://${window.location.hostname}:8000/celery-flow/ws`
  : `${window.location.protocol === 'https:' ? 'wss:' : 'ws:'}//${window.location.host}/celery-flow/ws`

export function useWebSocket(): UseWebSocketReturn {
  const [connectionStatus, setConnectionStatus] = useState<ConnectionStatus>('connecting')
  const [lastEvent, setLastEvent] = useState<TaskEvent | null>(null)
  const [events, setEvents] = useState<TaskEvent[]>([])
  const queryClient = useQueryClient()
  const wsRef = useRef<WebSocket | null>(null)
  const reconnectTimeoutRef = useRef<number | null>(null)

  const connect = useCallback(() => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
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
        console.log('[WebSocket] Disconnected, reconnecting in 3s...')

        // Reconnect after delay
        reconnectTimeoutRef.current = window.setTimeout(() => {
          connect()
        }, 3000)
      }

      ws.onerror = (error) => {
        setConnectionStatus('error')
        console.error('[WebSocket] Error:', error)
      }
    } catch (e) {
      setConnectionStatus('error')
      console.error('[WebSocket] Failed to connect:', e)
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

  return {
    connectionStatus,
    lastEvent,
    events,
  }
}

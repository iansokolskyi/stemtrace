import { useSyncExternalStore } from 'react'
import { isSessionRejected, subscribeSessionRejected } from '@/api/client'
import { useHealth } from '@/api/queries'
import { useWebSocketContext } from '@/hooks/WebSocketContext'

export interface LiveStatus {
  label: 'Live' | 'Connecting' | 'Polling' | 'Offline' | 'Unauthorized'
  tone: 'ok' | 'warn' | 'bad'
  detail: string
}

/** How the UI is getting updates. Priority: session rejected > server down > WS state. */
export function useLiveStatus(): LiveStatus {
  const { connectionStatus, isConnected } = useWebSocketContext()
  const { isError: isServerDown } = useHealth()
  const sessionRejected = useSyncExternalStore(subscribeSessionRejected, isSessionRejected)

  if (sessionRejected) {
    return {
      label: 'Unauthorized',
      tone: 'bad',
      detail: 'Session was signed by a different server process.',
    }
  }
  if (isServerDown) {
    return {
      label: 'Offline',
      tone: 'bad',
      detail: 'Server is unreachable. Check if stemtrace is running.',
    }
  }
  if (isConnected) {
    return { label: 'Live', tone: 'ok', detail: 'Real-time updates via WebSocket' }
  }
  if (connectionStatus === 'connecting') {
    return { label: 'Connecting', tone: 'warn', detail: 'Connecting to WebSocket...' }
  }
  return {
    label: 'Polling',
    tone: 'warn',
    detail: 'Updates every 5s. For real-time updates, configure WebSocket proxy.',
  }
}

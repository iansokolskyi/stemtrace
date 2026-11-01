import type { ReactElement } from 'react'
import type { Worker } from '@/api/client'
import { StatusMark } from '@/components/StatusMark'

interface WorkerStatusProps {
  status: Worker['status']
}

/**
 * A worker's status as icon plus word, never colour alone. Online is quiet; offline is the one
 * loud thing on the page (filled failure block, bold ink).
 */
export function WorkerStatus({ status }: WorkerStatusProps): ReactElement {
  const offline = status === 'offline'
  return (
    <StatusMark
      testId="worker-status"
      status={status}
      word={offline ? 'Offline' : 'Online'}
      icon={offline ? 'x' : 'check'}
      loud={offline}
      block="bg-failure text-on-failure"
    />
  )
}

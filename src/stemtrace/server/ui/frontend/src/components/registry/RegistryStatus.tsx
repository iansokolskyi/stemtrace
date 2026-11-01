import type { ReactElement } from 'react'
import type { TaskStatus } from '@/api/client'
import { StatusMark } from '@/components/StatusMark'
import { STATUS_LOOK } from './registry'

/** A task's registry status. Only not registered is loud (filled anomaly block, bold ink). */
export function RegistryStatus({ status }: { status: TaskStatus }): ReactElement {
  const { word, icon, loud } = STATUS_LOOK[status]
  return (
    <StatusMark
      testId="registry-status"
      status={status}
      word={word}
      icon={icon}
      loud={loud}
      block="bg-anomaly text-on-anomaly"
    />
  )
}

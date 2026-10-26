import type { ReactElement, ReactNode } from 'react'

interface PanelProps {
  title: string
  /** Muted text or a control on the right of the title. */
  aside?: ReactNode
  children: ReactNode
}

/** A titled surface on the task page: an 8px container with the title above its content. */
export function Panel({ title, aside, children }: PanelProps): ReactElement {
  return (
    <section className="mb-4 min-w-0 rounded-lg bg-surface">
      <div className="flex flex-wrap items-center gap-x-3.5 gap-y-1 px-4 pt-3.5 pb-2">
        <h2 className="text-sm font-bold">{title}</h2>
        {aside}
      </div>
      {children}
    </section>
  )
}

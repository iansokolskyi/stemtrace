import { type ReactElement, type ReactNode, useId } from 'react'

interface PanelProps {
  title: string
  /** Muted text or a control on the right of the title. */
  aside?: ReactNode
  children: ReactNode
}

/** A titled surface on the task page: an 8px container, named by its title for screen readers. */
export function Panel({ title, aside, children }: PanelProps): ReactElement {
  const titleId = useId()
  return (
    <section aria-labelledby={titleId} className="mb-4 min-w-0 rounded-lg bg-surface">
      <div className="flex flex-wrap items-center gap-x-3.5 gap-y-1 px-4 pt-3.5 pb-2">
        <h2 id={titleId} className="text-sm font-bold">
          {title}
        </h2>
        {aside}
      </div>
      {children}
    </section>
  )
}

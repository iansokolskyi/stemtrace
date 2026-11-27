import clsx from 'clsx'
import type { ReactElement, ReactNode } from 'react'

interface PageHeaderProps {
  title: ReactNode
  /** Muted line beside the title on wide screens, under it on narrow ones. */
  summary?: ReactNode
  /** Between the title and the summary, such as a status chip. */
  status?: ReactNode
  /** Pushed to the end of the row, such as buttons. */
  actions?: ReactNode
  /** Small line above the title (the module path on the Task page); stacks the header. */
  eyebrow?: ReactNode
  /** Rows under the title; stacks the header (the Task page). */
  children?: ReactNode
  /** Hostnames are identifiers, so the Worker page sets its title in mono. */
  mono?: boolean
  testId?: string
}

/** The title block of every page: 22 px / 800 title with its summary inline. */
export function PageHeader({
  title,
  summary,
  status,
  actions,
  eyebrow,
  children,
  mono = false,
  testId,
}: PageHeaderProps): ReactElement {
  const heading = (
    <h1
      tabIndex={-1}
      className={clsx(
        'm-0 min-w-0 text-page font-extrabold tracking-[-0.015em] text-ink [overflow-wrap:anywhere]',
        mono && 'font-mono',
      )}
    >
      {title}
    </h1>
  )
  if (eyebrow !== undefined || children !== undefined) {
    return (
      <header data-testid={testId} className="mb-4 flex min-w-0 flex-col items-start gap-2">
        {eyebrow}
        {heading}
        {children}
      </header>
    )
  }
  return (
    <header data-testid={testId} className="mb-4 flex flex-wrap items-center gap-x-3 gap-y-2">
      {heading}
      {status}
      {summary !== undefined && (
        <p className="m-0 min-h-5 text-small text-muted max-sm:basis-full">{summary}</p>
      )}
      {actions !== undefined && <span className="grow" />}
      {actions}
    </header>
  )
}

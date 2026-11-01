import { Link, useMatches } from '@tanstack/react-router'
import { Fragment, type ReactElement, type ReactNode } from 'react'
import { useTask } from '@/api/queries'
import { Icon } from '@/components/Icon'
import { useCarriedRange } from '@/hooks/useCarriedRange'

/** The characters of a task ID the breadcrumb shows (the full ID is its title). */
const SHORT_ID_LENGTH = 8

const LINK = 'text-muted hover:text-ink hover:underline'

interface Crumb {
  key: string
  node: ReactNode
}

/**
 * Where you are: the page's crumb from its route's staticData, and on a detail page the list
 * it belongs to (linked) followed by the item's ID. Executions filtered to one task name reads
 * "Executions > name"; a task page reads "Executions > task name (its runs) > short ID".
 */
export function Breadcrumbs(): ReactElement {
  const carried = useCarriedRange()
  const match = useMatches({ select: (matches) => matches[matches.length - 1] })
  const { crumb, section } = match?.staticData ?? {}
  const detail = Object.values((match?.params ?? {}) as Record<string, string>)[0]
  const isTask = match?.routeId === '/tasks/$taskId'
  // The same query the page runs, so no extra request; idle off the task page
  const { data: task } = useTask(isTask && detail ? detail : '')
  const taskName = task?.task.name
  const exactName =
    crumb === 'Executions'
      ? (match?.search as { name_exact?: string } | undefined)?.name_exact
      : undefined

  const crumbs: Crumb[] = []
  if (section && detail) {
    crumbs.push({
      key: 'section',
      node:
        section.to === '/executions' ? (
          <Link to="/executions" search={carried} className={LINK}>
            {section.label}
          </Link>
        ) : (
          <Link to={section.to} className={LINK}>
            {section.label}
          </Link>
        ),
    })
    if (isTask && taskName) {
      crumbs.push({
        key: 'name',
        node: (
          <Link
            to="/executions"
            search={{ name_exact: taskName, ...carried }}
            title={taskName}
            className={`${LINK} min-w-0 overflow-hidden text-ellipsis font-mono text-xs`}
          >
            {taskName}
          </Link>
        ),
      })
    }
    crumbs.push({
      key: 'last',
      node: <Last title={detail}>{isTask ? detail.slice(0, SHORT_ID_LENGTH) : detail}</Last>,
    })
  } else if (crumb && exactName) {
    crumbs.push(
      {
        key: 'section',
        node: (
          <Link to="/executions" search={carried} className={LINK}>
            {crumb}
          </Link>
        ),
      },
      { key: 'last', node: <Last title={exactName}>{exactName}</Last> },
    )
  } else if (crumb) {
    crumbs.push({
      key: 'crumb',
      node: (
        <span aria-current="page" className="font-bold">
          {crumb}
        </span>
      ),
    })
  }

  return (
    <nav
      aria-label="Breadcrumb"
      className="flex min-w-0 flex-1 items-center gap-1.5 overflow-hidden whitespace-nowrap text-[13px]"
    >
      {crumbs.map(({ key, node }, index) => (
        <Fragment key={key}>
          {index > 0 && <Icon name="chev" size={12} className="flex-none text-muted" />}
          {node}
        </Fragment>
      ))}
    </nav>
  )
}

/** The current page's crumb: mono, bold, cut with an ellipsis; `title` is the whole text. */
function Last({ title, children }: { title: string; children: ReactNode }): ReactElement {
  return (
    <span
      aria-current="page"
      title={title}
      className="min-w-0 overflow-hidden text-ellipsis font-mono text-xs font-bold"
    >
      {children}
    </span>
  )
}

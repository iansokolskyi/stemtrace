import { clsx } from 'clsx'
import { type ReactElement, useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { useTask } from '@/api/queries'
import { Icon } from '@/components/Icon'
import { useMediaQuery } from '@/hooks/useMediaQuery'
import { shortHost } from '@/utils/format'
import { AnomalyBody } from './AnomalyBody'
import { ExceptionBody } from './ExceptionBody'
import { exceptionOfId } from './exceptionId'
import { GroupBody } from './GroupBody'
import { openerKey, usePanel } from './PanelContext'
import type { PanelData } from './PanelParts'
import type { PanelEntry } from './panelState'
import { RunsBody } from './RunsBody'
import { decodeScope, scopeTitle } from './runsScope'
import { TaskBody } from './TaskBody'
import { WorkerBody } from './WorkerBody'

/** The entry's title (its heading and its crumb) and the muted line under the heading. */
function useEntryText(entry: PanelEntry): { title: string; sub?: string } {
  // The task page's own query, so a task row costs no request of its own
  const { data } = useTask(entry.kind === 'task' ? entry.id : '')
  switch (entry.kind) {
    case 'runs': {
      const scope = decodeScope(entry.id)
      if (!scope) return { title: 'Runs' }
      if (scope.kind === 'name') return { title: scopeTitle(scope), sub: scope.name }
      return {
        title: scopeTitle(scope),
        sub: scope.kind === 'kpi' ? undefined : 'Selected span, UTC',
      }
    }
    case 'exception':
      return { title: exceptionOfId(entry.id) ?? 'No exception class' }
    case 'worker':
      return { title: shortHost(entry.id), sub: entry.id }
    case 'group':
      return { title: entry.id }
    case 'task': {
      const name = data?.task.name.split('.').pop()
      return { title: `${name ?? 'Task'} ${entry.id.slice(0, 8)}`, sub: data?.task.name }
    }
    case 'anomaly':
      return { title: 'Anomaly' }
  }
}

function Body({ entry, data }: { entry: PanelEntry; data: PanelData }): ReactElement {
  switch (entry.kind) {
    case 'runs':
      return <RunsBody id={entry.id} data={data} />
    case 'exception':
      return <ExceptionBody id={entry.id} data={data} />
    case 'worker':
      return <WorkerBody id={entry.id} data={data} />
    case 'group':
      return <GroupBody id={entry.id} data={data} />
    case 'task':
      return <TaskBody id={entry.id} />
    case 'anomaly':
      return <AnomalyBody id={entry.id} />
  }
}

function Crumb({
  entry,
  current,
  onClick,
}: {
  entry: PanelEntry
  current: boolean
  onClick: () => void
}): ReactElement {
  const { title } = useEntryText(entry)
  return current ? (
    <span
      aria-current="page"
      title={title}
      className="max-w-[280px] truncate text-xs font-bold text-ink"
    >
      {title}
    </span>
  ) : (
    <>
      <button
        type="button"
        onClick={onClick}
        title={title}
        className="max-w-[150px] truncate text-xs text-muted hover:text-ink hover:underline"
      >
        {title}
      </button>
      <Icon name="chev" size={12} className="flex-none text-muted" />
    </>
  )
}

const ICON_BUTTON =
  'inline-flex size-8 flex-none items-center justify-center rounded-[3px] bg-raised text-ink hover:bg-press'

/**
 * The triage panel: an overlay drawer on the right (a full-screen sheet below 900px) over the
 * Overview, which never reflows. It shows the top of the stack in the `panel` URL value; rows push
 * deeper entries, the crumb (from two entries) and Esc go back, and × closes. The heading takes
 * focus on every change, and closing returns focus to the opener.
 */
export function TriagePanel({ data }: { data: PanelData }): ReactElement | null {
  const panel = usePanel()
  const isSheet = useMediaQuery('(max-width: 899px)')
  const [wide, setWide] = useState(false)
  const titleRef = useRef<HTMLHeadingElement>(null)
  const lastOpener = useRef<string | undefined>(undefined)

  const { stack } = panel
  const top = stack[stack.length - 1]
  const prev = stack[stack.length - 2]
  const topText = useEntryText(top ?? { kind: 'runs', id: '' })
  const prevText = useEntryText(prev ?? { kind: 'runs', id: '' })
  const open = stack.length > 0
  const topKey = top ? openerKey(top) : undefined

  // Esc goes back one level, unless a dialog (the search palette) or the drawer owns it
  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape' || e.defaultPrevented || document.querySelector('[role="dialog"]'))
        return
      e.preventDefault()
      panel.pop()
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [open, panel])

  // The heading takes focus when the panel opens and on every push or pop
  useEffect(() => {
    if (topKey) titleRef.current?.focus()
  }, [topKey])

  // Closing returns focus to what opened the panel (the chart when that is gone)
  useEffect(() => {
    if (open) {
      lastOpener.current = panel.activeKey
      return
    }
    const key = lastOpener.current
    if (key === undefined) return
    lastOpener.current = undefined
    const opener = document.querySelector<HTMLElement>(`[data-tk="${CSS.escape(key)}"]`)
    const fallback = document.querySelector<HTMLElement>('[data-testid="runs-chart"] svg')
    ;(opener ?? fallback)?.focus()
  }, [open, panel.activeKey])

  // A sheet covers the page: it cannot scroll or take focus behind it
  useEffect(() => {
    if (!open) return
    const page = document.querySelector<HTMLElement>('[data-frame-page]')
    document.documentElement.dataset.tri = '1'
    if (isSheet) page?.setAttribute('inert', '')
    return () => {
      delete document.documentElement.dataset.tri
      page?.removeAttribute('inert')
    }
  }, [open, isSheet])

  if (!open || !top) return null
  const width = wide ? '.8' : '.46'
  const max = wide ? '1100px' : '720px'

  return createPortal(
    <aside
      aria-label="Triage panel"
      data-testid="triage-panel"
      style={
        isSheet ? undefined : { width: `clamp(420px, calc((100vw - 244px) * ${width}), ${max})` }
      }
      className={clsx(
        'fixed flex flex-col bg-surface',
        isSheet ? 'inset-0 z-50' : 'top-[52px] right-0 bottom-0 z-28 rounded-tl-lg shadow-side',
      )}
    >
      <div className="flex min-h-12 flex-none items-center gap-2 py-2 pr-3 pl-4">
        {isSheet && (
          <button
            type="button"
            onClick={panel.pop}
            aria-label={`Back to ${prev ? prevText.title : 'Overview'}`}
            className="inline-flex h-[30px] items-center gap-1.5 rounded-[3px] bg-raised px-3 text-xs font-semibold hover:bg-press"
          >
            <Icon name="back" size={14} />
            Back
          </button>
        )}
        {stack.length < 2 ? (
          <div className="min-w-0 flex-1" />
        ) : (
          <nav aria-label="Triage path" className="min-w-0 flex-1 overflow-hidden">
            <ol className="m-0 flex list-none items-center gap-1 p-0 whitespace-nowrap">
              {stack.map((entry, i) => (
                <li
                  key={`${openerKey(entry)}#${i}`}
                  className="inline-flex min-w-0 items-center gap-1"
                >
                  <Crumb
                    entry={entry}
                    current={i === stack.length - 1}
                    onClick={() => panel.popTo(i + 1)}
                  />
                </li>
              ))}
            </ol>
          </nav>
        )}
        {!isSheet && (
          <button
            type="button"
            aria-pressed={wide}
            onClick={() => setWide(!wide)}
            className="inline-flex h-[30px] items-center rounded-[3px] bg-raised px-3 text-xs font-semibold hover:bg-press aria-pressed:bg-ink aria-pressed:text-on-ink"
          >
            {wide ? 'Collapse' : 'Expand'}
          </button>
        )}
        <button
          type="button"
          onClick={panel.close}
          aria-label="Close triage panel (Esc goes back one level)"
          title="Close"
          className={ICON_BUTTON}
        >
          <Icon name="x" size={16} />
        </button>
      </div>
      <div className="min-h-0 flex-1 overflow-auto px-4 pt-1.5 pb-8">
        <h2
          ref={titleRef}
          tabIndex={-1}
          className="m-0 text-lg leading-snug font-extrabold tracking-[-0.01em] [overflow-wrap:anywhere] focus:outline-none focus-visible:outline-2 focus-visible:outline-offset-[3px] focus-visible:outline-ink"
        >
          {topText.title}
        </h2>
        {topText.sub && (
          <div className="text-xs text-muted [overflow-wrap:anywhere]">{topText.sub}</div>
        )}
        <Body entry={top} data={data} />
      </div>
    </aside>,
    document.body,
  )
}

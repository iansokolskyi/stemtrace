import { useQuery } from '@tanstack/react-query'
import { useNavigate } from '@tanstack/react-router'
import { clsx } from 'clsx'
import {
  type KeyboardEvent,
  type ReactElement,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
} from 'react'
import { fetchTask, HttpError } from '@/api/client'
import { registrySnapshot, useGraphs, workersSnapshot } from '@/api/queries'
import { useCarriedRange } from '@/hooks/useCarriedRange'
import { type PaletteItem, type PaletteKind, paletteItems, TASK_ID_SHAPE } from './paletteItems'

/** How long the query rests before a task ID is looked up, so typing sends one request. */
const LOOKUP_DELAY_MS = 250

/** The server's task IDs are lower-case and its lookup is case-sensitive: retry once lower-case. */
async function lookupTask(id: string): ReturnType<typeof fetchTask> {
  try {
    return await fetchTask(id)
  } catch (error) {
    const lower = id.toLowerCase()
    if (error instanceof HttpError && error.status === 404 && lower !== id) {
      return fetchTask(lower)
    }
    throw error
  }
}

interface CommandPaletteProps {
  open: boolean
  onClose: () => void
}

/**
 * The ⌘K / Ctrl+K search dialog: a task ID, a task name, a flow or a worker, by keyboard. It
 * reads what the page already loaded and never asks Celery to inspect (`refresh=false`).
 * Focus returns to the element that opened it when it closes.
 */
export function CommandPalette({ open, onClose }: CommandPaletteProps): ReactElement | null {
  return open ? <PaletteDialog onClose={onClose} /> : null
}

/** "a", "a and b", "a, b and c". */
function joinWords(words: string[]): string {
  return words.length < 2 ? words.join('') : `${words.slice(0, -1).join(', ')} and ${words.at(-1)}`
}

function PaletteDialog({ onClose }: { onClose: () => void }): ReactElement {
  const navigate = useNavigate()
  const carried = useCarriedRange()
  const id = useId()
  const [query, setQuery] = useState('')
  const [selectedKey, setSelectedKey] = useState<string | null>(null)
  const [opener] = useState(() => document.activeElement)

  const inputRef = useRef<HTMLInputElement>(null)
  // Focus the search box; on any close, give focus back to whatever had it before the dialog
  // opened. One effect, so React's dev-mode remount focuses the box again.
  useEffect(() => {
    inputRef.current?.focus()
    return () => {
      if (opener instanceof HTMLElement) opener.focus()
    }
  }, [opener])

  const names = useQuery({ ...registrySnapshot, retry: false })
  const workers = useQuery({ ...workersSnapshot, retry: false })
  const flows = useGraphs(undefined, { retry: false })

  const text = query.trim()
  const [rested, setRested] = useState('')
  useEffect(() => {
    const timer = setTimeout(() => setRested(text), LOOKUP_DELAY_MS)
    return () => clearTimeout(timer)
  }, [text])
  const lookupId = TASK_ID_SHAPE.test(text) && rested === text ? text : ''
  const lookup = useQuery({
    queryKey: ['tasks', lookupId],
    queryFn: () => lookupTask(lookupId),
    enabled: lookupId !== '',
    retry: false,
  })
  const waitingForId = TASK_ID_SHAPE.test(text) && (rested !== text || lookup.isFetching)
  // A 404 is an answer ("no such task"); any other error means the lookup itself failed
  const lookupFailed =
    lookupId !== '' &&
    lookup.isError &&
    !(lookup.error instanceof HttpError && lookup.error.status === 404)

  const items = useMemo(
    () =>
      paletteItems(query, {
        names: names.data?.tasks,
        flows: flows.data?.graphs,
        workers: workers.data?.workers,
        task: lookupId ? lookup.data?.task : null,
      }),
    [query, names.data, flows.data, workers.data, lookupId, lookup.data],
  )

  // The selection follows its item when the list changes (an ID lookup lands on top); it falls
  // back to the first row when its item is gone.
  const selected = Math.max(
    0,
    items.findIndex((item) => item.key === selectedKey),
  )
  const optionId = (index: number) => `${id}-option-${index}`

  // Enter pressed while the ID lookup is still out: act on the task row when it lands
  const enterWaiting = useRef(false)
  // biome-ignore lint/correctness/useExhaustiveDependencies: `choose` only reads values already listed here
  useEffect(() => {
    if (!enterWaiting.current || waitingForId) return
    enterWaiting.current = false
    const found = items.find((item) => item.kind === 'task')
    if (found) choose(found)
  }, [waitingForId, items])

  function choose(item: PaletteItem | undefined): void {
    if (!item) return
    onClose()
    switch (item.kind) {
      case 'task':
        navigate({ to: '/tasks/$taskId', params: { taskId: item.id } })
        break
      case 'name':
        navigate({ to: '/executions', search: { name_exact: item.id, ...carried } })
        break
      case 'flow':
        navigate({ to: '/flows/$rootId', params: { rootId: item.id } })
        break
      case 'worker':
        navigate({ to: '/workers/$hostname', params: { hostname: item.id } })
        break
    }
  }

  function onKeyDown(event: KeyboardEvent): void {
    if (event.key === 'Escape') {
      // Only this dialog closes: the drawer's own Escape listener must not see it
      event.stopPropagation()
      event.preventDefault()
      onClose()
    } else if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault()
      if (items.length === 0) return
      const step = event.key === 'ArrowDown' ? 1 : items.length - 1
      const next = (selected + step) % items.length
      setSelectedKey(items[next].key)
      document.getElementById(optionId(next))?.scrollIntoView({ block: 'nearest' })
    } else if (event.key === 'Enter') {
      // An Enter that commits an IME composition belongs to the IME
      if (event.nativeEvent.isComposing) return
      event.preventDefault()
      if (waitingForId) enterWaiting.current = true
      else choose(items[selected])
    } else if (event.key === 'Tab') {
      // The dialog holds focus: the only stop is the search box
      event.preventDefault()
    }
  }

  const groups: {
    kind: PaletteKind
    heading: string
    items: { item: PaletteItem; index: number }[]
  }[] = []
  items.forEach((item, index) => {
    const last = groups[groups.length - 1]
    if (last?.heading === item.group) last.items.push({ item, index })
    else groups.push({ kind: item.kind, heading: item.group, items: [{ item, index }] })
  })

  const loading = names.isPending || workers.isPending || flows.isPending
  const failed = [
    names.isError && 'task names',
    flows.isError && 'flows',
    workers.isError && 'workers',
    lookupFailed && 'that task ID',
  ].filter(Boolean)
  let emptyText = ''
  if (failed.length > 0) {
    // Not "No results": the answer may be in a source that did not load
    emptyText = `Couldn't load ${joinWords(failed as string[])}`
  } else if (items.length === 0) {
    if (waitingForId || (loading && !text)) emptyText = 'Searching…'
    else emptyText = text ? `No results for “${text}”` : 'No flows or workers yet'
  }

  return (
    <div
      className="fixed inset-0 z-[100] flex items-start justify-center bg-scrim px-4 pt-[12vh] pb-4"
      onMouseDown={(event) => {
        if (event.target !== event.currentTarget) return
        // The press would otherwise move focus to the page after the dialog gave it back
        event.preventDefault()
        onClose()
      }}
      onKeyDown={onKeyDown}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-label="Search"
        className="w-[min(620px,100%)] overflow-hidden rounded-lg bg-surface shadow-pop"
      >
        <label htmlFor={`${id}-input`} className="sr-only">
          Search task IDs, task names, flows and workers
        </label>
        <input
          id={`${id}-input`}
          ref={inputRef}
          type="text"
          role="combobox"
          aria-expanded={items.length > 0}
          aria-controls={`${id}-list`}
          aria-autocomplete="list"
          aria-activedescendant={items.length > 0 ? optionId(selected) : undefined}
          autoComplete="off"
          spellCheck={false}
          placeholder="Search task ID, task name, flow or worker…"
          value={query}
          onChange={(event) => {
            setQuery(event.target.value)
            enterWaiting.current = false
            setSelectedKey(null)
          }}
          className="h-[46px] w-full rounded-none border-0 bg-raised px-4 text-sm text-ink placeholder:text-muted focus-visible:rounded-none focus-visible:shadow-[inset_0_-3px_0_var(--ink)] focus-visible:outline-none!"
        />
        <div
          id={`${id}-list`}
          role="listbox"
          aria-label="Results"
          className={clsx(
            'max-h-[min(50vh,420px)] overflow-auto p-1.5',
            items.length === 0 && 'hidden',
          )}
        >
          {groups.map((group) => (
            // biome-ignore lint/a11y/useSemanticElements: a listbox may only contain group and option, not a fieldset
            <div key={group.kind} role="group" aria-labelledby={`${id}-group-${group.kind}`}>
              <div
                id={`${id}-group-${group.kind}`}
                className="px-2.5 pt-2 pb-1 text-xs font-bold text-muted"
              >
                {group.heading}
              </div>
              {group.items.map(({ item, index }) => (
                <div
                  key={item.key}
                  id={optionId(index)}
                  role="option"
                  tabIndex={-1}
                  aria-selected={index === selected}
                  data-kind={item.kind}
                  // Keep focus in the search box when a row is pressed
                  onMouseDown={(event) => event.preventDefault()}
                  onClick={() => choose(item)}
                  // Focus stays in the search box, which owns the keys; this covers a row that gets it
                  onKeyDown={(event) => {
                    if (event.key !== 'Enter' && event.key !== ' ') return
                    event.preventDefault()
                    event.stopPropagation()
                    choose(item)
                  }}
                  className="flex w-full cursor-pointer items-center gap-2.5 rounded-[3px] px-2.5 py-2 hover:bg-hover aria-selected:bg-raised aria-selected:outline-2 aria-selected:-outline-offset-2 aria-selected:outline-ink"
                >
                  <span
                    className={clsx(
                      'min-w-0 truncate text-[12.5px] font-semibold',
                      item.kind === 'task' && 'font-mono',
                    )}
                  >
                    {item.label}
                  </span>
                  <span className="ml-auto max-w-[45%] truncate text-xs whitespace-nowrap text-muted">
                    {item.desc}
                  </span>
                </div>
              ))}
            </div>
          ))}
        </div>
        {emptyText && (
          <div role="status" className="px-4 py-3.5 text-muted">
            {emptyText}
          </div>
        )}
        <div className="flex gap-3.5 px-4 py-2 text-xs text-muted" aria-hidden="true">
          <span>↑↓ navigate</span>
          <span>↵ open</span>
          <span>esc close</span>
        </div>
      </div>
    </div>
  )
}

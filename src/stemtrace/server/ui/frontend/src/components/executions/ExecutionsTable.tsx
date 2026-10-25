import { Link } from '@tanstack/react-router'
import { useVirtualizer } from '@tanstack/react-virtual'
import { clsx } from 'clsx'
import { type ReactElement, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import type { ExecutionRow, FetchExecutionsParams } from '@/api/client'
import { useExecutions } from '@/api/queries'
import { StatusChip } from '../StatusChip'
import { TaskName } from '../TaskName'
import { type Column, columnsFor } from './columns'
import { type Density, ROW_HEIGHT } from './density'
import { useFrozenRows } from './useFrozenRows'

/** Load the next page when the last visible row is this close to the end of the loaded rows. */
const LOAD_AHEAD_ROWS = 10
/** Space left under the table box, so its bottom edge is not flush with the window. */
const BOTTOM_GAP = 24
const MIN_BOX_HEIGHT = 240

interface ExecutionsTableProps {
  params: FetchExecutionsParams
  density: Density
}

/**
 * The Executions table. It remounts when the filters change, which resets what a page load
 * decides once (the column set).
 */
export function ExecutionsTable({ params, density }: ExecutionsTableProps): ReactElement {
  return <LoadedTable key={JSON.stringify(params)} params={params} density={density} />
}

/**
 * The room under the box's top edge, kept current as the window or the content above resizes:
 * the box is as tall as the window allows and scrolls inside, so a long list never makes the
 * page scroll and the virtualiser has a bounded element to measure.
 */
function useBoxMaxHeight(boxRef: React.RefObject<HTMLDivElement | null>): number {
  // Bounded from the first render: an unbounded box would show every row, and the virtualiser
  // would load page after page before the real height is measured
  const [height, setHeight] = useState(MIN_BOX_HEIGHT)
  useLayoutEffect(() => {
    const measure = () => {
      const box = boxRef.current
      if (!box) return
      const top = box.getBoundingClientRect().top + window.scrollY
      setHeight(Math.max(MIN_BOX_HEIGHT, Math.floor(window.innerHeight - top - BOTTOM_GAP)))
    }
    measure()
    window.addEventListener('resize', measure)
    const observer = new ResizeObserver(measure)
    observer.observe(document.body)
    return () => {
      window.removeEventListener('resize', measure)
      observer.disconnect()
    }
  }, [boxRef])
  return height
}

function LoadedTable({ params, density }: ExecutionsTableProps): ReactElement {
  const { data, isLoading, error, hasNextPage, isFetchingNextPage, fetchNextPage } =
    useExecutions(params)

  const rows = useMemo(() => {
    const seen = new Set<string>()
    const all: ExecutionRow[] = []
    for (const page of data?.pages ?? []) {
      for (const task of page.tasks) {
        // An update that lands between two page requests can list a task twice
        if (seen.has(task.task_id)) continue
        seen.add(task.task_id)
        all.push(task)
      }
    }
    return all
  }, [data])
  const total = data?.pages[0]?.total ?? 0

  // The columns are decided once, from the first page that has rows, and kept for this page
  // load: later pages and live refetches never add or drop one, so the table never re-lays out.
  const [columns, setColumns] = useState<Column[] | null>(null)
  const firstPage = data?.pages[0]?.tasks
  if (columns === null && firstPage && firstPage.length > 0) setColumns(columnsFor(firstPage))

  if (isLoading) return <Notice>Loading executions...</Notice>
  if (error) return <Notice tone="error">Failed to load tasks. Is the server running?</Notice>
  if (!rows.length || !columns) {
    return (
      <Notice>
        <span className="block text-sm font-semibold text-ink">No tasks found</span>
        Try adjusting your filters or run some Celery tasks.
      </Notice>
    )
  }

  return (
    <TableBox
      rows={rows}
      total={total}
      columns={columns}
      density={density}
      hasNextPage={hasNextPage}
      isFetchingNextPage={isFetchingNextPage}
      fetchNextPage={fetchNextPage}
    />
  )
}

function Notice({ children, tone }: { children: React.ReactNode; tone?: 'error' }): ReactElement {
  return (
    <div
      className={clsx(
        'rounded-lg bg-surface px-4 py-10 text-center text-xs',
        tone === 'error' ? 'font-semibold text-failure-t' : 'text-muted',
      )}
    >
      {children}
    </div>
  )
}

interface TableBoxProps {
  rows: ExecutionRow[]
  total: number
  columns: Column[]
  density: Density
  hasNextPage: boolean | undefined
  isFetchingNextPage: boolean
  fetchNextPage: () => void
}

function TableBox({
  rows: liveRows,
  total,
  columns,
  density,
  hasNextPage,
  isFetchingNextPage,
  fetchNextPage,
}: TableBoxProps): ReactElement {
  const boxRef = useRef<HTMLDivElement>(null)
  const maxHeight = useBoxMaxHeight(boxRef)

  // While the pointer is over the table, or it is scrolled away from its top, the order the
  // reader is looking at stays put: updates land in the rows already shown, and tasks that
  // would be inserted above them wait behind the "N new" button.
  const [hovered, setHovered] = useState(false)
  const [scrolled, setScrolled] = useState(false)
  const { shown: rows, newCount, apply } = useFrozenRows(liveRows, hovered || scrolled)
  const showNew = () => {
    apply()
    boxRef.current?.scrollTo({ top: 0 })
  }
  const rowHeight = ROW_HEIGHT[density]

  const virtualizer = useVirtualizer({
    count: rows.length,
    getScrollElement: () => boxRef.current,
    estimateSize: () => rowHeight,
    getItemKey: (index) => rows[index].task_id,
    overscan: 8,
  })
  // A new density changes every row's height
  // biome-ignore lint/correctness/useExhaustiveDependencies: rowHeight is the trigger
  useEffect(() => virtualizer.measure(), [rowHeight, virtualizer])

  const items = virtualizer.getVirtualItems()
  const lastIndex = items.length ? items[items.length - 1].index : -1
  useEffect(() => {
    if (lastIndex >= rows.length - LOAD_AHEAD_ROWS && hasNextPage && !isFetchingNextPage) {
      fetchNextPage()
    }
  }, [lastIndex, rows.length, hasNextPage, isFetchingNextPage, fetchNextPage])

  const template = columns.map((column) => column.track).join(' ')
  const minWidth = columns.reduce((sum, column) => sum + column.min, 0)

  return (
    <div>
      <div className="flex h-8 items-center justify-between text-xs text-muted">
        <span>
          Showing {rows.length} of {total} tasks
        </span>
        {/* The slot is always there, so the button appearing never moves the table */}
        {newCount > 0 && (
          <button
            type="button"
            onClick={showNew}
            className="h-6 rounded-[3px] bg-ink px-2.5 text-xs font-semibold text-on-ink"
          >
            {newCount} new
          </button>
        )}
      </div>
      <div
        ref={boxRef}
        data-testid="executions-scroll"
        className="overflow-auto rounded-lg bg-surface"
        style={{ maxHeight }}
        onPointerEnter={() => setHovered(true)}
        onPointerLeave={() => setHovered(false)}
        onScroll={(event) => setScrolled(event.currentTarget.scrollTop > 0)}
      >
        <div style={{ minWidth }}>
          <div
            className="sticky top-0 z-10 grid items-center border-b border-rule bg-surface text-xs font-semibold text-muted"
            style={{ gridTemplateColumns: template, height: 36 }}
          >
            {columns.map((column, index) => (
              <div
                key={column.id}
                data-column-header={column.id}
                className={cellClass(column, index)}
              >
                {column.label}
              </div>
            ))}
          </div>
          <div className="relative" style={{ height: virtualizer.getTotalSize() }}>
            {items.map((item) => {
              const row = rows[item.index]
              return (
                <Link
                  key={item.key}
                  to="/tasks/$taskId"
                  params={{ taskId: row.task_id }}
                  className="absolute left-0 grid w-full items-center border-b border-rule text-xs text-ink hover:bg-hover"
                  style={{
                    gridTemplateColumns: template,
                    height: item.size,
                    transform: `translateY(${item.start}px)`,
                  }}
                >
                  {columns.map((column, index) => (
                    <Cell key={column.id} column={column} row={row} first={index === 0} />
                  ))}
                </Link>
              )
            })}
          </div>
        </div>
      </div>
    </div>
  )
}

function cellClass(column: Column, index: number): string {
  return clsx(
    'min-w-0 truncate px-3',
    index === 0 && 'pl-4',
    column.align === 'right' && 'text-right tabular-nums',
  )
}

function Cell({
  column,
  row,
  first,
}: {
  column: Column
  row: ExecutionRow
  first: boolean
}): ReactElement {
  const className = cellClass(column, first ? 0 : 1)
  switch (column.id) {
    case 'state':
      return (
        <div className={className}>
          <StatusChip state={row.state} title={row.exception_type ?? undefined} />
        </div>
      )
    case 'name':
      return (
        <div className={className}>
          <TaskName name={row.name} />
        </div>
      )
    default: {
      const text = column.text?.(row) ?? ''
      const mono = column.id === 'worker' || column.id === 'queue'
      return (
        <div
          data-cell={column.id}
          title={column.id === 'worker' ? (row.hostname ?? undefined) : undefined}
          className={clsx(
            className,
            mono && 'font-mono',
            (column.id === 'updated' || column.id === 'queue_wait') && 'text-muted',
          )}
        >
          {text}
        </div>
      )
    }
  }
}

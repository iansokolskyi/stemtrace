import { Link } from '@tanstack/react-router'
import { clsx } from 'clsx'
import type { CSSProperties, ReactElement, ReactNode } from 'react'
import type { OverviewResponse } from '@/api/client'
import { Icon, type IconName } from '@/components/Icon'
import { openerProps, usePanel } from '@/components/triage/PanelContext'
import { encodeScope, type RunsScope } from '@/components/triage/runsScope'
import type { Host } from '@/components/workers/workers'
import { formatCount, formatPercent, formatSpan, shortHost } from '@/utils/format'
import { type Bucket, baselineNote, formatRate, type Grade, gradeOf } from './model'
import { Sparkline } from './Sparkline'
import { AnomaliesTileSlot } from './slots'

/** How many tiles the row holds: 5 once the Anomalies tile fills its slot. */
export const TILE_COUNT: number = 4

const TILE =
  'flex min-w-0 flex-col gap-[3px] rounded-lg bg-surface px-3.5 pt-3.5 pb-2.5 text-left text-ink hover:bg-[color-mix(in_srgb,var(--ink)_6%,var(--surface))] data-[on]:outline-2 data-[on]:-outline-offset-2 data-[on]:outline-ink'

const VALUE_TONE: Record<Grade, string> = { ok: '', warn: 'text-retry-t', bad: 'text-failure-t' }
const NOTE_BLOCK: Record<Exclude<Grade, 'ok'>, string> = {
  warn: 'retry-hatch text-on-retry',
  bad: 'bg-failure text-on-failure',
}

interface TileFaceProps {
  label: string
  value: ReactNode
  grade: Grade
  note: string
  noteIcon: IconName
  sub: string
  spark?: ReactNode
}

/** What every tile shows: label, value, the note (loud only when off baseline), the earlier value, a graphic. */
export function TileFace({
  label,
  value,
  grade,
  note,
  noteIcon,
  sub,
  spark,
}: TileFaceProps): ReactElement {
  return (
    <>
      <span className="block text-xs font-semibold text-muted">{label}</span>
      <span
        className={clsx(
          'block text-[26px] leading-[1.15] font-extrabold tracking-[-0.02em]',
          VALUE_TONE[grade],
        )}
      >
        {value}
      </span>
      <span
        className={clsx(
          'flex min-h-4 items-center gap-1.5 text-xs',
          grade === 'ok' ? 'text-muted' : 'font-bold text-ink',
        )}
      >
        {grade !== 'ok' && (
          <span
            className={clsx(
              'inline-flex size-4 flex-none items-center justify-center rounded-[3px]',
              NOTE_BLOCK[grade],
            )}
          >
            <Icon name={noteIcon} size={12} className="[stroke-width:2.5]" />
          </span>
        )}
        {note}
      </span>
      <span className="flex min-h-4 items-center text-xs text-muted">{sub}</span>
      <span className="mt-1.5 block h-[26px]">{spark}</span>
    </>
  )
}

interface KpiTilesProps {
  overview: OverviewResponse | undefined
  buckets: Bucket[]
  hosts: Host[] | undefined
  /** The Overview's window (`6h`); none for All. */
  within: string | undefined
}

/**
 * The tiles over the chart: each a number for the window, how far it is from the window before
 * it, and a sparkline of the same series the chart draws. A run tile opens its runs in the
 * triage panel. Fixed height, so a tile never moves when its numbers arrive.
 */
export function KpiTiles({ overview, buckets, hosts, within }: KpiTilesProps): ReactElement {
  const panel = usePanel()
  const compare = within !== undefined
  const style = {
    '--tiles': TILE_COUNT,
    '--tiles-md': TILE_COUNT === 5 ? 3 : TILE_COUNT,
  } as CSSProperties

  const runsTile = (() => {
    const kpi = overview?.runs_per_minute
    const grade = kpi && compare ? gradeOf(kpi.value, kpi.baseline, 1.4, 2, true) : 'ok'
    const scope: RunsScope = { kind: 'kpi', within, label: 'Runs' }
    return (
      <button
        type="button"
        {...openerProps(panel, { kind: 'runs', id: encodeScope(scope) })}
        className={TILE}
      >
        <TileFace
          label="Runs / min"
          value={kpi ? formatRate(kpi.value) : ''}
          grade={grade}
          note={kpi && compare ? baselineNote(grade, kpi.value, kpi.baseline) : ''}
          noteIcon={kpi && kpi.value > kpi.baseline ? 'up' : 'down'}
          sub={kpi && compare ? `earlier: ${formatRate(kpi.baseline)}/min` : ''}
          spark={<Sparkline values={buckets.map((b) => b.outcomes)} height={26} grade={grade} />}
        />
      </button>
    )
  })()

  const failuresTile = (() => {
    const kpi = overview?.failures
    const grade = kpi && compare ? gradeOf(kpi.value, kpi.baseline, 1.3, 2) : 'ok'
    const scope: RunsScope = { kind: 'kpi', within, state: 'FAILURE', label: 'Failures' }
    return (
      <button
        type="button"
        {...openerProps(panel, { kind: 'runs', id: encodeScope(scope) })}
        className={TILE}
      >
        <TileFace
          label="Failures"
          value={kpi ? formatCount(kpi.value) : ''}
          grade={grade}
          note={kpi && compare ? baselineNote(grade, kpi.value, kpi.baseline) : ''}
          noteIcon="up"
          sub={kpi && compare ? `earlier: ${formatCount(kpi.baseline)}` : ''}
          spark={<Sparkline values={buckets.map((b) => b.failure)} height={26} grade={grade} />}
        />
      </button>
    )
  })()

  const rateTile = (() => {
    const kpi = overview?.failure_rate
    const grade = kpi && compare ? gradeOf(kpi.value, kpi.baseline, 1.3, 2) : 'ok'
    const scope: RunsScope = { kind: 'kpi', within, state: 'FAILURE', label: 'Failure rate' }
    return (
      <button
        type="button"
        {...openerProps(panel, { kind: 'runs', id: encodeScope(scope) })}
        className={TILE}
      >
        <TileFace
          label="Failure rate"
          value={kpi ? formatPercent(kpi.value) : ''}
          grade={grade}
          note={kpi && compare ? baselineNote(grade, kpi.value, kpi.baseline) : ''}
          noteIcon="up"
          sub={kpi && compare ? `earlier: ${formatPercent(kpi.baseline)}` : ''}
          spark={
            <Sparkline
              values={buckets.map((b) => (b.outcomes ? b.failure / b.outcomes : 0))}
              height={26}
              grade={grade}
            />
          }
        />
      </button>
    )
  })()

  return (
    <div
      data-testid="kpi-tiles"
      style={style}
      className="mb-4 grid grid-cols-2 gap-3 min-[900px]:grid-cols-[repeat(var(--tiles-md),minmax(0,1fr))] min-[1280px]:grid-cols-[repeat(var(--tiles),minmax(0,1fr))]"
    >
      {runsTile}
      {failuresTile}
      {rateTile}
      <WorkersTile hosts={hosts} overview={overview} />
      <AnomaliesTileSlot className={TILE} />
    </div>
  )
}

function WorkersTile({
  hosts,
  overview,
}: {
  hosts: Host[] | undefined
  overview: OverviewResponse | undefined
}): ReactElement {
  const offline = (hosts ?? []).filter((h) => h.leading.status === 'offline')
  const total = hosts?.length ?? overview?.workers.total ?? 0
  const online = hosts ? total - offline.length : (overview?.workers.online ?? 0)
  const first = offline[0]
  const grade: Grade = offline.length ? 'bad' : 'ok'
  const note = first
    ? `${offline.length} offline: ${shortHost(first.hostname)} for ${formatSpan(Date.now() - Date.parse(first.leading.last_seen))}`
    : 'All online'
  return (
    <Link to="/workers" className={TILE} data-testid="workers-tile">
      <TileFace
        label="Workers online"
        value={hosts || overview ? `${online}/${total}` : ''}
        grade={grade}
        note={hosts || overview ? note : ''}
        noteIcon="x"
        sub=""
        spark={
          <span className="flex gap-[3px]" aria-hidden="true">
            {(hosts ?? []).map((h) => (
              <i
                key={h.hostname}
                title={`${h.hostname} ${h.leading.status}`}
                className={clsx(
                  'h-3.5 flex-1 rounded-[2px]',
                  h.leading.status === 'online' ? 'bg-success' : 'bg-failure',
                )}
              />
            ))}
          </span>
        }
      />
    </Link>
  )
}

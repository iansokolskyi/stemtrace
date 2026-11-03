import { Link } from '@tanstack/react-router'
import { clsx } from 'clsx'
import type { CSSProperties, ReactElement } from 'react'
import type { OverviewResponse } from '@/api/client'
import { openerProps, usePanel } from '@/components/triage/PanelContext'
import { encodeScope, type RunsScope } from '@/components/triage/runsScope'
import type { Host } from '@/components/workers/workers'
import { formatCount, formatPercent, formatSpan, shortHost } from '@/utils/format'
import { AnomaliesTile } from './AnomaliesTile'
import type { OverviewAnomalies } from './anomalyData'
import { type Bucket, baselineNote, formatRate, type Grade, gradeOf } from './model'
import { Sparkline } from './Sparkline'
import { TILE, TileFace } from './TileFace'

/** How many tiles the row holds; the grid is laid out for it before any data arrives. */
const TILE_COUNT = 5

interface KpiTilesProps {
  overview: OverviewResponse | undefined
  buckets: Bucket[]
  hosts: Host[] | undefined
  /** The Overview's window (`6h`); none for All. */
  within: string | undefined
  anomalies: OverviewAnomalies | undefined
  anomaliesFailed: boolean
}

/**
 * The tiles over the chart: each a number for the window, how far it is from the window before
 * it, and a sparkline of the same series the chart draws. A run tile opens its runs in the
 * triage panel. Fixed height, so a tile never moves when its numbers arrive.
 */
export function KpiTiles({
  overview,
  buckets,
  hosts,
  within,
  anomalies,
  anomaliesFailed,
}: KpiTilesProps): ReactElement {
  const panel = usePanel()
  const compare = within !== undefined
  const style = {
    '--tiles': TILE_COUNT,
    '--tiles-md': 3,
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
      <AnomaliesTile anomalies={anomalies} failed={anomaliesFailed} className={TILE} />
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

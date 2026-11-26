import { createFileRoute, redirect } from '@tanstack/react-router'
import { useEffect, useMemo } from 'react'
import { UnauthorizedError } from '@/api/client'
import {
  CHART_BUCKET,
  overviewWithin,
  statsWithin,
  useFailures,
  useGroupSeries,
  useOverview,
  useRunsSeries,
  useWindowTaskStats,
  useWorkersStrip,
} from '@/api/overviewQueries'
import { useOverviewAnomalies } from '@/components/overview/anomalyData'
import { formatBrush, parseBrush } from '@/components/overview/brushSpan'
import { HealthTable } from '@/components/overview/HealthTable'
import { KpiTiles } from '@/components/overview/KpiTiles'
import { LoadError, type LoadFailure } from '@/components/overview/LoadError'
import { bucketsOf } from '@/components/overview/model'
import { type Brush, RunsChart } from '@/components/overview/RunsChart'
import { StaleNotice } from '@/components/overview/StaleNotice'
import { WhatsWrong } from '@/components/overview/WhatsWrong'
import { WorkerStrip } from '@/components/overview/WorkerStrip'
import { PanelProvider } from '@/components/triage/PanelContext'
import type { PanelData } from '@/components/triage/PanelParts'
import { clock } from '@/components/triage/runsScope'
import { TriagePanel } from '@/components/triage/TriagePanel'
import { hostsOf } from '@/components/workers/workers'
import { carryRange } from '@/hooks/useCarriedRange'
import { DEFAULT_RANGE, legacyExecutionsSearch, overviewSearch, WINDOW_WORDS } from './-search'

export const Route = createFileRoute('/')({
  validateSearch: overviewSearch,
  // An old task-list link (`/?state=FAILURE`) keeps its filters on Executions; the range and the
  // triage panel alone are the Overview's.
  beforeLoad: ({ location }) => {
    const legacy = legacyExecutionsSearch(location.search as Record<string, unknown>)
    if (legacy) throw redirect({ to: '/executions', search: legacy, replace: true })
  },
  component: OverviewPage,
  staticData: { crumb: 'Overview', timeRange: true },
})

function OverviewPage() {
  const { range, brush: brushValue } = Route.useSearch()
  const navigate = Route.useNavigate()
  const effective = range ?? DEFAULT_RANGE
  const isAll = effective === 'all'
  const within = statsWithin(effective)
  const windowWords = WINDOW_WORDS[effective]

  // Executions links carry the range the Overview last showed, as from Executions itself
  useEffect(() => carryRange(range), [range])

  const overview = useOverview(overviewWithin(effective))
  const series = useRunsSeries(within, isAll ? undefined : CHART_BUCKET)
  const failures = useFailures(within)
  const stats = useWindowTaskStats(within)
  const groupSeries = useGroupSeries(within, !isAll)
  const workers = useWorkersStrip()
  const { data: anomalies, failed: anomaliesFailed, stale: anomaliesStale } = useOverviewAnomalies()

  // Data held from an earlier answer wins over a failed refresh (it shows, with the stale line);
  // an error shows only for a part that never loaded
  const firstLoadFailure = (q: {
    isError: boolean
    data: unknown
    error: unknown
    refetch: () => unknown
  }): LoadFailure | undefined =>
    q.isError && q.data === undefined ? { error: q.error, retry: q.refetch } : undefined
  const seriesStatus = series.data ? 'ready' : series.isError ? 'failed' : 'loading'
  // An expired session (401) is the login redirect's to handle, not a stale page
  const refreshFailed = [overview, series, failures, stats, workers].filter(
    (q) => q.isError && q.data !== undefined && !(q.error instanceof UnauthorizedError),
  )
  const retryRefresh = () => Promise.allSettled(refreshFailed.map((q) => q.refetch()))

  const overviewFailure = firstLoadFailure(overview)
  const buckets = useMemo(() => (series.data ? bucketsOf(series.data) : []), [series.data])
  const hosts = useMemo(
    () => (workers.data ? hostsOf(workers.data.workers) : undefined),
    [workers.data],
  )
  // The brush is in the URL as absolute instants. A change replaces the history entry: a drag
  // refines the view, so Back leaves the page's state instead of stepping through brushes.
  const brush = useMemo(() => parseBrush(brushValue), [brushValue])
  const setBrush = (next: Brush | null) =>
    navigate({ search: (prev) => ({ ...prev, brush: formatBrush(next) }), replace: true })

  const data: PanelData = {
    buckets,
    seriesStatus: seriesStatus,
    failures: failures.data,
    stats: stats.data,
    hosts,
    anomalies,
    range: effective,
    within,
  }

  // The window's end is known only once the overview answers; until then the subtitle is just the span
  const subtitle = isAll
    ? 'All runs held'
    : [
        windowWords.replace('the last', 'Last'),
        overview.data && `ending ${clock(overview.data.window.end)} UTC`,
      ]
        .filter(Boolean)
        .join(', ')

  return (
    <PanelProvider>
      <div>
        <div className="mb-4">
          <h1 tabIndex={-1} className="text-2xl font-bold text-ink outline-none">
            Overview
          </h1>
          <p className="mt-1 min-h-5 text-sm text-muted">{subtitle}</p>
        </div>
        {refreshFailed.length > 0 && (
          <StaleNotice
            since={Math.min(...refreshFailed.map((q) => q.dataUpdatedAt))}
            onRetry={retryRefresh}
          />
        )}
        {overviewFailure && (
          <div className="mb-4 rounded-lg bg-surface">
            <LoadError what="the overview" failure={overviewFailure} />
          </div>
        )}
        <KpiTiles
          overview={overview.data}
          buckets={buckets}
          hosts={hosts}
          within={within}
          anomalies={anomalies}
          anomaliesFailed={anomaliesFailed}
        />
        <RunsChart
          status={series.data ? 'ready' : series.isError ? 'error' : 'loading'}
          failure={firstLoadFailure(series)}
          buckets={buckets}
          bucketSeconds={series.data?.bucket_seconds}
          brush={brush}
          onBrush={setBrush}
        />
        <WhatsWrong
          groups={failures.data?.groups}
          failed={firstLoadFailure(failures)}
          stale={(failures.isError && failures.data !== undefined) || anomaliesStale}
          windowWords={windowWords}
          anomalies={anomalies}
        />
        <WorkerStrip hosts={hosts} failed={firstLoadFailure(workers)} />
        <HealthTable
          failed={firstLoadFailure(stats)}
          stats={stats.data}
          series={groupSeries.data}
          within={within}
          windowWords={windowWords}
        />
      </div>
      <TriagePanel data={data} />
    </PanelProvider>
  )
}

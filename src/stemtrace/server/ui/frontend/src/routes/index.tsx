import { createFileRoute, redirect } from '@tanstack/react-router'
import { useEffect, useMemo, useState } from 'react'
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
import { HealthTable } from '@/components/overview/HealthTable'
import { KpiTiles } from '@/components/overview/KpiTiles'
import { bucketsOf } from '@/components/overview/model'
import { type Brush, RunsChart } from '@/components/overview/RunsChart'
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
  const { range } = Route.useSearch()
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
  const { data: anomalies, failed: anomaliesFailed } = useOverviewAnomalies()

  const buckets = useMemo(() => (series.data ? bucketsOf(series.data) : []), [series.data])
  const hosts = useMemo(
    () => (workers.data ? hostsOf(workers.data.workers) : undefined),
    [workers.data],
  )
  // The brush is a view of this page only: Back restores the panel from the URL, not the span
  const [brush, setBrush] = useState<Brush | null>(null)

  const data: PanelData = {
    buckets,
    seriesStatus: series.isError
      ? ('failed' as const)
      : series.data
        ? ('ready' as const)
        : ('loading' as const),
    failures: failures.data,
    stats: stats.data,
    hosts,
    anomalies,
    range: effective,
    within,
  }

  const ending = overview.data ? ` ending ${clock(overview.data.window.end)} UTC` : ''

  return (
    <PanelProvider>
      <div>
        <div className="mb-4">
          <h1 className="text-2xl font-bold text-ink">Overview</h1>
          <p className="mt-1 min-h-5 text-sm text-muted">
            {isAll ? 'All runs held' : `${windowWords.replace('the last', 'Last')},${ending}`}
          </p>
        </div>
        <KpiTiles
          overview={overview.data}
          buckets={buckets}
          hosts={hosts}
          within={within}
          anomalies={anomalies}
          anomaliesFailed={anomaliesFailed}
        />
        <RunsChart
          buckets={buckets}
          bucketSeconds={series.data?.bucket_seconds}
          brush={brush}
          onBrush={setBrush}
        />
        <WhatsWrong
          groups={failures.data?.groups}
          windowWords={windowWords}
          anomalies={anomalies}
        />
        <WorkerStrip hosts={hosts} />
        <HealthTable
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

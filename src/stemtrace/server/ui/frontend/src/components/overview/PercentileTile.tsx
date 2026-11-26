import type { ReactElement } from 'react'
import type { PercentileKpi, RunSort } from '@/api/client'
import { openerProps, usePanel } from '@/components/triage/PanelContext'
import { encodeScope, type RunsScope } from '@/components/triage/runsScope'
import { formatCount, formatSpan } from '@/utils/format'
import { baselineNote, gradeOf, P95_MIN_SAMPLES } from './model'
import { Sparkline } from './Sparkline'
import { TILE, TileFace } from './TileFace'

interface PercentileTileProps {
  label: string
  /** The order its runs open in: the sample the tile takes its p95 from. */
  sort: RunSort
  /** What the panel calls the runs it opens. */
  panelLabel: string
  /** `warn` and `bad` ratios of a rise over the window before. */
  thresholds: readonly [number, number]
  /** What the note says when no run gave a sample. */
  emptyNote: string
  kpi: PercentileKpi | undefined
  /** The p95 per bucket, when the series has it. */
  series: readonly (number | null)[] | undefined
  /** The Overview's window (`6h`); none for All, which compares nothing. */
  within: string | undefined
}

/**
 * A p95 tile: the window's p95, graded against the window before it only when both windows hold
 * enough runs, and a click that opens the runs behind it, slowest first.
 */
export function PercentileTile({
  label,
  sort,
  panelLabel,
  thresholds,
  emptyNote,
  kpi,
  series,
  within,
}: PercentileTileProps): ReactElement {
  const panel = usePanel()
  const scope: RunsScope = { kind: 'kpi', within, label: panelLabel, sort }
  const [warn, bad] = thresholds

  let value = ''
  let note = ''
  let sub = ''
  let grade: ReturnType<typeof gradeOf> = 'ok'
  if (kpi && kpi.value === null) {
    value = 'No data'
    note = emptyNote
  } else if (kpi?.value != null) {
    value = formatSpan(kpi.value)
    const comparable =
      within !== undefined &&
      kpi.baseline !== null &&
      kpi.samples >= P95_MIN_SAMPLES &&
      kpi.baseline_samples >= P95_MIN_SAMPLES
    if (comparable && kpi.baseline !== null) {
      grade = gradeOf(kpi.value, kpi.baseline, warn, bad)
      note = baselineNote(grade, kpi.value, kpi.baseline)
      sub = `earlier: ${formatSpan(kpi.baseline)}`
    } else {
      sub = `from ${formatCount(kpi.samples)} ${kpi.samples === 1 ? 'run' : 'runs'}`
    }
  }

  return (
    <button
      type="button"
      {...openerProps(panel, { kind: 'runs', id: encodeScope(scope) })}
      className={TILE}
      data-grade={grade}
    >
      <TileFace
        label={label}
        value={value}
        grade={grade}
        note={note}
        noteIcon="up"
        sub={sub}
        spark={series ? <Sparkline values={series} height={26} grade={grade} /> : undefined}
      />
    </button>
  )
}

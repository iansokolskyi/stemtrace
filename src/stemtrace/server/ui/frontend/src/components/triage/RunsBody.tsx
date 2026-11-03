import { Link } from '@tanstack/react-router'
import { type ReactElement, useState } from 'react'
import { formatCount, formatPercent } from '@/utils/format'
import { BUTTON_QUIET, Figures, type PanelData, Section } from './PanelParts'
import { RunRows } from './RunRows'
import { decodeScope, scopeFilters } from './runsScope'

const STATE_CHIPS = ['FAILURE', 'RETRY', 'STARTED', 'PENDING', 'SUCCESS'] as const

interface RunsBodyProps {
  id: string
  data: PanelData
}

/**
 * The runs of a bar, a brushed span, a KPI tile or one task name: the full-rate counts of the
 * span when the chart has them, then the runs themselves with the state chips that narrow them.
 */
export function RunsBody({ id, data }: RunsBodyProps): ReactElement {
  const scope = decodeScope(id)
  const [chosen, setChosen] = useState<string[]>([])
  if (!scope)
    return <p className="m-0 pt-4 text-xs text-muted">This link does not name any runs.</p>

  const span =
    scope.from && scope.to
      ? data.buckets.filter((b) => b.start >= (scope.from ?? '') && b.start < (scope.to ?? ''))
      : []
  const success = span.reduce((sum, b) => sum + b.success, 0)
  const failure = span.reduce((sum, b) => sum + b.failure, 0)
  const retry = span.reduce((sum, b) => sum + b.retry, 0)
  const outcomes = span.reduce((sum, b) => sum + b.outcomes, 0)

  const filters = scopeFilters(scope)
  // A tile fixes its state; the chips narrow a span or a name
  const chipsShown = !scope.state
  if (chosen.length) filters.state = chosen.join(',')

  const wholeWindow = scope.kind === 'kpi' || scope.kind === 'name'
  const toExecutions = wholeWindow
    ? {
        state: scope.state,
        name_exact: scope.name,
        range: scope.within === '1h' || scope.within === '6h' ? scope.within : ('all' as const),
      }
    : null

  return (
    <>
      {span.length > 0 && (
        <Section>
          <Figures
            items={[
              { label: 'Success', value: formatCount(success) },
              { label: 'Failure', value: formatCount(failure), loud: failure > 0 },
              { label: 'Retry', value: formatCount(retry) },
              { label: 'Failure rate', value: formatPercent(outcomes ? failure / outcomes : 0) },
            ]}
          />
        </Section>
      )}
      <Section title="Runs">
        {chipsShown && (
          <fieldset
            aria-label="Filter by state"
            className="m-0 mb-2 flex min-w-0 flex-wrap gap-1.5 border-0 p-0"
          >
            <Chip pressed={chosen.length === 0} onClick={() => setChosen([])}>
              All
            </Chip>
            {STATE_CHIPS.map((state) => (
              <Chip
                key={state}
                pressed={chosen.includes(state)}
                onClick={() =>
                  setChosen(
                    chosen.includes(state) ? chosen.filter((s) => s !== state) : [...chosen, state],
                  )
                }
              >
                {state.charAt(0) + state.slice(1).toLowerCase()}
              </Chip>
            ))}
          </fieldset>
        )}
        <RunRows filters={filters} />
        {toExecutions && (
          <div className="mt-2.5">
            <Link to="/executions" search={toExecutions} className={BUTTON_QUIET}>
              Open in Executions
            </Link>
          </div>
        )}
      </Section>
    </>
  )
}

function Chip({
  pressed,
  onClick,
  children,
}: {
  pressed: boolean
  onClick: () => void
  children: string
}): ReactElement {
  return (
    <button
      type="button"
      aria-pressed={pressed}
      onClick={onClick}
      className="inline-flex h-[26px] items-center rounded-[3px] bg-raised px-2.5 text-xs font-semibold text-muted hover:bg-press hover:text-ink aria-pressed:bg-ink aria-pressed:text-on-ink"
    >
      {children}
    </button>
  )
}

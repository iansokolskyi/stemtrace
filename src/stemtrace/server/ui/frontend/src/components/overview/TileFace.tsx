import { clsx } from 'clsx'
import type { ReactElement, ReactNode } from 'react'
import { Icon, type IconName } from '@/components/Icon'
import type { Grade } from './model'

/** A tile's grade: off baseline (`warn`, `bad`) or, for the Anomalies tile, `anomaly`. */
export type TileGrade = Grade | 'anomaly'

export const TILE =
  'flex min-w-0 flex-col gap-[3px] rounded-lg bg-surface px-3.5 pt-3.5 pb-2.5 text-left text-ink hover:bg-[color-mix(in_srgb,var(--ink)_6%,var(--surface))] data-[on]:outline-2 data-[on]:-outline-offset-2 data-[on]:outline-ink'

const VALUE_TONE: Record<TileGrade, string> = {
  ok: '',
  warn: 'text-retry-t',
  bad: 'text-failure-t',
  anomaly: 'text-anomaly-t',
}
const NOTE_BLOCK: Record<Exclude<TileGrade, 'ok'>, string> = {
  anomaly: 'bg-anomaly text-on-anomaly',
  warn: 'retry-hatch text-on-retry',
  bad: 'bg-failure text-on-failure',
}

interface TileFaceProps {
  label: string
  value: ReactNode
  grade: TileGrade
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
          'flex min-h-8 items-start gap-1.5 text-xs',
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
        <span title={note} className="line-clamp-2 min-w-0">
          {note}
        </span>
      </span>
      <span className="flex min-h-4 min-w-0 items-center text-xs text-muted">
        <span title={sub} className="truncate">
          {sub}
        </span>
      </span>
      <span className="mt-1.5 block h-[26px]">{spark}</span>
    </>
  )
}

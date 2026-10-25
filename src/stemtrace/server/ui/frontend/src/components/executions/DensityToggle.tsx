import type { ReactElement } from 'react'
import type { Density } from './density'

const OPTIONS: { value: Density; label: string }[] = [
  { value: 'compact', label: 'Compact' },
  { value: 'comfortable', label: 'Comfortable' },
]

interface DensityToggleProps {
  value: Density
  onChange: (density: Density) => void
}

/** A two-way segmented control for how tightly the table rows are packed. */
export function DensityToggle({ value, onChange }: DensityToggleProps): ReactElement {
  return (
    <fieldset
      aria-label="Row density"
      className="m-0 min-w-0 border-0 inline-flex flex-none gap-0.5 rounded-[3px] bg-raised p-0.5"
    >
      {OPTIONS.map((option) => (
        <button
          key={option.value}
          type="button"
          aria-pressed={value === option.value}
          onClick={() => onChange(option.value)}
          className="h-[26px] rounded-[2px] px-[11px] text-xs font-semibold text-muted hover:bg-hover hover:text-ink aria-pressed:bg-ink aria-pressed:text-on-ink aria-pressed:hover:bg-ink aria-pressed:hover:text-on-ink"
        >
          {option.label}
        </button>
      ))}
    </fieldset>
  )
}

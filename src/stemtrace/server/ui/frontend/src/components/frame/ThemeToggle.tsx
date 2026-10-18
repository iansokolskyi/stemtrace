import type { ReactElement } from 'react'
import { Icon } from '@/components/Icon'
import { useTheme } from '@/hooks/useTheme'

/** Switches between the light and dark themes; the choice overrides the OS and is remembered. */
export function ThemeToggle(): ReactElement {
  const { theme, setTheme } = useTheme()
  const next = theme === 'dark' ? 'light' : 'dark'
  return (
    <button
      type="button"
      onClick={() => setTheme(next)}
      aria-label={`Switch to ${next} theme`}
      className="inline-flex h-[30px] items-center gap-1.5 whitespace-nowrap rounded-[3px] bg-raised px-3 text-xs font-semibold hover:bg-press active:translate-y-px"
    >
      <Icon name={next === 'dark' ? 'moon' : 'sun'} />
      {next === 'dark' ? 'Dark' : 'Light'}
    </button>
  )
}

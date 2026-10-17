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
      title={`Switch to ${next} theme`}
      className="inline-flex size-8 flex-none items-center justify-center rounded-[3px] bg-raised text-ink hover:bg-press"
    >
      <Icon name={next === 'dark' ? 'moon' : 'sun'} size={16} />
    </button>
  )
}

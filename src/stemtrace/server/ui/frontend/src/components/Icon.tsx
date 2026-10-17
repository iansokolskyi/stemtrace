import type { ReactElement } from 'react'

/**
 * The Signal box icon set: stroked 24px glyphs drawn for stemtrace (ported from the approved
 * prototype), so the UI depends on no icon library. Decorative: the adjacent text or the
 * control's accessible name carries the meaning.
 */
const PATHS = {
  check: (
    <>
      <path d="M5 12.5l4.5 4.5L19 7.5" />
    </>
  ),
  x: (
    <>
      <path d="M6 6l12 12M18 6L6 18" />
    </>
  ),
  retry: (
    <>
      <path d="M20 11a8 8 0 10-2.3 5.7M20 4v7h-7" />
    </>
  ),
  play: (
    <>
      <path d="M8 5l11 7-11 7z" />
    </>
  ),
  clock: (
    <>
      <circle cx="12" cy="12" r="8.5" />
      <path d="M12 7.5V12l3 2" />
    </>
  ),
  down: (
    <>
      <path d="M12 5v13M6 13l6 6 6-6" />
    </>
  ),
  alert: (
    <>
      <path d="M12 4l9 16H3z" />
      <path d="M12 10v4M12 17.3v.2" />
    </>
  ),
  search: (
    <>
      <circle cx="11" cy="11" r="6.5" />
      <path d="M16 16l4.5 4.5" />
    </>
  ),
  menu: (
    <>
      <path d="M4 7h16M4 12h16M4 17h16" />
    </>
  ),
  back: (
    <>
      <path d="M15 5l-7 7 7 7" />
    </>
  ),
  chev: (
    <>
      <path d="M9 5l7 7-7 7" />
    </>
  ),
  sun: (
    <>
      <circle cx="12" cy="12" r="4" />
      <path d="M12 3v2M12 19v2M3 12h2M19 12h2M5.6 5.6l1.4 1.4M17 17l1.4 1.4M5.6 18.4L7 17M17 7l1.4-1.4" />
    </>
  ),
  moon: (
    <>
      <path d="M20 14.5A8 8 0 019.5 4 8 8 0 1020 14.5z" />
    </>
  ),
  copy: (
    <>
      <rect x="8.5" y="8.5" width="11" height="11" rx="2" />
      <path d="M15.5 8.5V6.5a2 2 0 00-2-2h-7a2 2 0 00-2 2v7a2 2 0 002 2h2" />
    </>
  ),
  plus: (
    <>
      <path d="M12 5v14M5 12h14" />
    </>
  ),
  grid: (
    <>
      <rect x="4" y="4" width="6.5" height="6.5" rx="1.5" />
      <rect x="13.5" y="4" width="6.5" height="6.5" rx="1.5" />
      <rect x="4" y="13.5" width="6.5" height="6.5" rx="1.5" />
      <rect x="13.5" y="13.5" width="6.5" height="6.5" rx="1.5" />
    </>
  ),
  list: (
    <>
      <path d="M8 6h12M8 12h12M8 18h12M4 6h.01M4 12h.01M4 18h.01" />
    </>
  ),
  flow: (
    <>
      <circle cx="6" cy="6" r="2.5" />
      <circle cx="6" cy="18" r="2.5" />
      <circle cx="18" cy="12" r="2.5" />
      <path d="M8.5 6H12a3 3 0 013 3v.5M8.5 18H12a3 3 0 003-3v-.5" />
    </>
  ),
  server: (
    <>
      <rect x="4" y="4" width="16" height="6.5" rx="2" />
      <rect x="4" y="13.5" width="16" height="6.5" rx="2" />
      <path d="M8 7.2h.01M8 16.7h.01" />
    </>
  ),
  book: (
    <>
      <path d="M5 5.5A1.5 1.5 0 016.5 4H19v14H6.5A1.5 1.5 0 005 19.5z" />
      <path d="M5 19.5A1.5 1.5 0 006.5 21H19v-3" />
    </>
  ),
  jump: (
    <>
      <circle cx="12" cy="12" r="3" />
      <path d="M12 3v4M12 17v4M3 12h4M17 12h4" />
    </>
  ),
  minus: (
    <>
      <path d="M5 12h14" />
    </>
  ),
  filter: (
    <>
      <path d="M4 5h16l-6 7.5V19l-4-2v-4.5z" />
    </>
  ),
  up: (
    <>
      <path d="M12 19V6M6 11l6-6 6 6" />
    </>
  ),
} satisfies Record<string, ReactElement>

export type IconName = keyof typeof PATHS

interface IconProps {
  name: IconName
  /** Rendered width and height in pixels. */
  size?: number
  className?: string
}

export function Icon({ name, size = 14, className }: IconProps): ReactElement {
  return (
    <svg
      className={className}
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
    >
      {PATHS[name]}
    </svg>
  )
}

import type { ReactElement } from 'react'

interface SproutMarkProps {
  /** Rendered width and height in pixels. */
  size?: number
  className?: string
}

/**
 * The stemtrace "Sprout" mark (dark variant), drawn inline so it needs no
 * network request and works under any mount prefix. Decorative only: the
 * adjacent wordmark carries the accessible name.
 *
 * Source of truth: docs/brand/stemtrace-mark.svg (full-size geometry, stroke 2.6,
 * r=2.6). The favicon copies in index.html and templates/login.html deliberately
 * use thicker strokes (3.2, r=3) for 16px legibility.
 */
export function SproutMark({ size = 22, className }: SproutMarkProps): ReactElement {
  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      viewBox="0 0 32 32"
      width={size}
      height={size}
      className={className}
      aria-hidden="true"
      focusable="false"
    >
      <rect x="1" y="1" width="30" height="30" rx="8" fill="#123A2B" />
      <path
        d="M16 26V15M16 15L9.5 8.5M16 15l6.5-6.5"
        stroke="#46D39A"
        strokeWidth="2.6"
        strokeLinecap="round"
        fill="none"
      />
      <circle cx="16" cy="26" r="2.6" fill="#46D39A" />
      <circle cx="9" cy="8" r="2.6" fill="#E7ECF2" />
      <circle cx="23" cy="8" r="2.6" fill="#E7ECF2" />
    </svg>
  )
}

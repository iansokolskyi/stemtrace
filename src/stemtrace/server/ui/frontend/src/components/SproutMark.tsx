import type { ReactElement } from 'react'

interface SproutMarkProps {
  /** Rendered width and height in pixels. */
  size?: number
  className?: string
}

/**
 * The stemtrace Sprout mark as drawn for the Signal box UI: ink stems that fork like the
 * flow graph's tracks, with two leaves in --sprout. Inline, so it needs no request and works
 * under any mount prefix. Decorative: the adjacent wordmark carries the accessible name.
 *
 * The favicon (index.html, templates/login.html) keeps the brand mark from docs/brand/.
 */
export function SproutMark({ size = 30, className }: SproutMarkProps): ReactElement {
  return (
    <svg
      viewBox="0 0 34 34"
      width={size}
      height={size}
      className={className}
      aria-hidden="true"
      focusable="false"
    >
      <g
        fill="none"
        stroke="var(--ink)"
        strokeWidth="2.7"
        strokeLinecap="round"
        strokeLinejoin="round"
      >
        <path d="M17 29.5C16.3 25 17.5 20.5 16.8 15.2" />
        <path d="M16.9 20C13.6 19.2 10.8 16.2 9.9 11" />
        <path d="M16.9 15.6C19.7 14.6 22.2 12 23.6 7.6" />
      </g>
      <g fill="var(--sprout)">
        <ellipse cx="9.4" cy="8.4" rx="2.3" ry="3.7" transform="rotate(-18 9.4 8.4)" />
        <ellipse cx="24.1" cy="5.4" rx="2.3" ry="3.7" transform="rotate(20 24.1 5.4)" />
      </g>
    </svg>
  )
}

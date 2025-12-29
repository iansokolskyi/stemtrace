/**
 * Simple tooltip component with instant hover display.
 * Supports different placements to avoid going out of bounds.
 */

import type { ReactNode } from 'react'

type TooltipPlacement = 'top' | 'bottom' | 'left' | 'right'

interface TooltipProps {
  content: string
  placement?: TooltipPlacement
  children: ReactNode
}

const placementStyles: Record<TooltipPlacement, { container: string; arrow: string }> = {
  top: {
    container: 'bottom-full left-1/2 -translate-x-1/2 mb-2',
    arrow:
      'top-full left-1/2 -translate-x-1/2 border-t-slate-800 border-x-transparent border-b-transparent',
  },
  bottom: {
    container: 'top-full left-1/2 -translate-x-1/2 mt-2',
    arrow:
      'bottom-full left-1/2 -translate-x-1/2 border-b-slate-800 border-x-transparent border-t-transparent',
  },
  left: {
    container: 'right-full top-1/2 -translate-y-1/2 mr-2',
    arrow:
      'left-full top-1/2 -translate-y-1/2 border-l-slate-800 border-y-transparent border-r-transparent',
  },
  right: {
    container: 'left-full top-1/2 -translate-y-1/2 ml-2',
    arrow:
      'right-full top-1/2 -translate-y-1/2 border-r-slate-800 border-y-transparent border-l-transparent',
  },
}

export function Tooltip({ content, placement = 'bottom', children }: TooltipProps) {
  const styles = placementStyles[placement]

  return (
    <div className="relative group">
      {children}
      <div
        className={`absolute ${styles.container} px-2 py-1 text-xs text-slate-200 bg-slate-800 rounded shadow-lg whitespace-nowrap opacity-0 invisible group-hover:opacity-100 group-hover:visible transition-opacity duration-150 pointer-events-none z-50`}
      >
        {content}
        {/* Arrow */}
        <div className={`absolute ${styles.arrow} border-4`} />
      </div>
    </div>
  )
}

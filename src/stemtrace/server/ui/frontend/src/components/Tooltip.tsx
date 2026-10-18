/**
 * Simple tooltip with instant hover display, in the raised tone with the tinted pop shadow.
 * Placements keep it inside the viewport next to edges.
 */

import type { ReactNode } from 'react'

type TooltipPlacement = 'top' | 'top-start' | 'bottom' | 'bottom-end' | 'left' | 'right'

interface TooltipProps {
  content: string
  placement?: TooltipPlacement
  children: ReactNode
}

const placementStyles: Record<TooltipPlacement, string> = {
  top: 'bottom-full left-1/2 -translate-x-1/2 mb-2',
  // Left-aligned above the trigger: for triggers near the left edge (the sidebar footer)
  'top-start': 'bottom-full left-0 mb-2',
  bottom: 'top-full left-1/2 -translate-x-1/2 mt-2',
  // Right-aligned below the trigger: for triggers near the right edge
  'bottom-end': 'top-full right-0 mt-2',
  left: 'right-full top-1/2 -translate-y-1/2 mr-2',
  right: 'left-full top-1/2 -translate-y-1/2 ml-2',
}

export function Tooltip({ content, placement = 'bottom', children }: TooltipProps) {
  return (
    <div className="relative group">
      {children}
      <div
        className={`absolute ${placementStyles[placement]} pointer-events-none invisible z-50 w-max max-w-64 rounded-lg bg-raised px-2.5 py-1.5 text-xs font-normal text-ink opacity-0 shadow-pop transition-opacity duration-150 group-hover:visible group-hover:opacity-100`}
      >
        {content}
      </div>
    </div>
  )
}

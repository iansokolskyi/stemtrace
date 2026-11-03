import type { ReactElement } from 'react'

/**
 * The body of an `anomaly` entry. The Anomalies work (`feat/ui-anomalies`) owns the anomaly API
 * types and hook; once it is merged into `redesign`, this shows the anomaly's explanation and
 * its task or registry entry. Until then no opener creates such an entry, so a link that names
 * one lands here.
 */
export function AnomalyBody({ id }: { id: string }): ReactElement {
  return (
    <p className="m-0 pt-4 text-xs text-muted [overflow-wrap:anywhere]">
      Details for the anomaly {id} are not available in this view.
    </p>
  )
}

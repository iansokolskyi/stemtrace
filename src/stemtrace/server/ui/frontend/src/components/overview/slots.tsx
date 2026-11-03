import type { ReactElement } from 'react'

/**
 * Where the anomalies come in. The Anomalies work (`feat/ui-anomalies`) owns the anomaly API
 * types and hook; once it is merged into `redesign`, these two slots are filled with that hook
 * and `KpiTiles`' `TILE_COUNT` becomes 5. Until then nothing about anomalies is drawn.
 */

/** The Anomalies tile of the KPI row. */
export function AnomaliesTileSlot(_props: { className: string }): ReactElement | null {
  return null
}

/** The anomalies half of "What's wrong", beside failures by exception. */
export function AnomaliesWrongSlot(): ReactElement | null {
  return null
}

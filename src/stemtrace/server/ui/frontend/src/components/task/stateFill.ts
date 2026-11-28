/**
 * The fill of a bar or swatch for a Celery state: success, failure, retry (hatched, see
 * `.retry-hatch` in index.css), running, and grey for everything else. Full class names, so
 * Tailwind sees them.
 */
export function stateFill(state: string): string {
  switch (state) {
    case 'SUCCESS':
      return 'bg-success'
    case 'FAILURE':
      return 'bg-failure'
    case 'RETRY':
      return 'retry-hatch'
    case 'STARTED':
      return 'bg-running'
    default:
      return 'bg-pending'
  }
}

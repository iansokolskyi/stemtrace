import { useState } from 'react'

interface NextPageQuery {
  isFetchNextPageError: boolean
  error: unknown
  isFetchingNextPage: boolean
  fetchNextPage: () => Promise<{ isFetchNextPageError: boolean; error: unknown }>
}

export interface NextPage {
  /** A next page failed and nobody has retried it: the list says so and stops asking. */
  failed: boolean
  error: unknown
  busy: boolean
  retry: () => Promise<unknown>
}

/**
 * The failure of an infinite list's next page, kept apart from the first page's refresh. The
 * query clears its own next-page error when a poll succeeds, which would re-arm the scroll
 * sentinel and ask for the failed page again on every poll; here only Retry clears it.
 * `key` is the list's filters: another list starts clean.
 */
export function useNextPage(query: NextPageQuery, key: string): NextPage {
  const [kept, setKept] = useState<{ key: string; error: unknown } | null>(null)
  if (query.isFetchNextPageError && kept?.key !== key) setKept({ key, error: query.error })
  const failed = kept !== null && kept.key === key
  return {
    failed,
    error: kept?.error,
    busy: query.isFetchingNextPage,
    retry: async () => {
      const result = await query.fetchNextPage()
      setKept(result.isFetchNextPageError ? { key, error: result.error } : null)
    },
  }
}

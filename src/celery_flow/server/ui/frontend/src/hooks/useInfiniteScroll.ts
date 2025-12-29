/**
 * Hook for infinite scroll behavior with intersection observer.
 */

import { useCallback, useEffect, useRef } from 'react'

interface UseInfiniteScrollOptions {
  hasNextPage: boolean | undefined
  isFetchingNextPage: boolean
  fetchNextPage: () => void
  rootMargin?: string
}

export function useInfiniteScroll({
  hasNextPage,
  isFetchingNextPage,
  fetchNextPage,
  rootMargin = '200px',
}: UseInfiniteScrollOptions) {
  const observerRef = useRef<IntersectionObserver | null>(null)

  const lastElementRef = useCallback(
    (node: HTMLElement | null) => {
      if (isFetchingNextPage) return

      if (observerRef.current) {
        observerRef.current.disconnect()
      }

      observerRef.current = new IntersectionObserver(
        (entries) => {
          if (entries[0].isIntersecting && hasNextPage) {
            fetchNextPage()
          }
        },
        { rootMargin },
      )

      if (node) {
        observerRef.current.observe(node)
      }
    },
    [isFetchingNextPage, hasNextPage, fetchNextPage, rootMargin],
  )

  // Cleanup on unmount
  useEffect(() => {
    return () => {
      if (observerRef.current) {
        observerRef.current.disconnect()
      }
    }
  }, [])

  return { lastElementRef }
}

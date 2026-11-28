import { getLoginPath, getLoginUrl } from '@/api/client'

/**
 * Shown instead of page content when the server rejects the session because it
 * was signed by a different secret, so the user gets an actionable message
 * rather than "Failed to load ...".
 */
export function SessionRejectedNotice() {
  const loginPath = getLoginPath()

  return (
    <div role="alert" className="rounded-lg bg-surface px-4 py-10 text-center">
      <p className="m-0 text-body font-medium text-failure-t">
        Your session was rejected because it was signed by a different server process (another
        worker or replica).
      </p>
      <p className="mx-auto mt-2 mb-0 max-w-xl text-small text-muted">
        If stemtrace runs with several workers or replicas, set STEMTRACE_LOGIN_SECRET to the same
        long random value on all of them.
      </p>
      {loginPath && (
        <a href={getLoginUrl(loginPath)} className="link mt-4 inline-block text-small">
          Sign in again
        </a>
      )}
    </div>
  )
}

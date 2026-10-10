import { getLoginPath, getLoginUrl } from '@/api/client'

/**
 * Shown instead of page content when the server rejects the session because it
 * was signed by a different secret, so the user gets an actionable message
 * rather than "Failed to load ...".
 */
export function SessionRejectedNotice() {
  const loginPath = getLoginPath()

  return (
    <div role="alert" className="text-center py-12">
      <p className="text-red-400">
        Your session was rejected because it was signed by a different server process (another
        worker or replica).
      </p>
      <p className="text-sm text-slate-400 mt-2">
        If stemtrace runs with several workers or replicas, set STEMTRACE_LOGIN_SECRET to the same
        long random value on all of them.
      </p>
      {loginPath && (
        <a
          href={getLoginUrl(loginPath)}
          className="text-blue-400 hover:underline mt-4 inline-block"
        >
          Sign in again
        </a>
      )}
    </div>
  )
}

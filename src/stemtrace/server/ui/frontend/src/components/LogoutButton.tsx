/**
 * Header Logout control, shown only when built-in form login is enabled
 * (the server then injects the logout endpoint path).
 */
export function LogoutButton() {
  const logoutPath = window.__STEMTRACE_LOGOUT_PATH__
  if (!logoutPath) return null

  // Logout is a POST endpoint; a plain form keeps it working without JS state.
  return (
    <form method="post" action={logoutPath} className="m-0">
      <button
        type="submit"
        className="text-xs text-slate-400 hover:text-slate-100 border border-slate-700 hover:border-slate-500 rounded-full px-3 py-1 transition-colors"
      >
        Logout
      </button>
    </form>
  )
}

/**
 * Logout control, shown only when built-in form login is enabled (the server then injects the
 * logout endpoint path).
 */
export function LogoutButton() {
  const logoutPath = window.__STEMTRACE_LOGOUT_PATH__
  if (!logoutPath) return null

  // Logout is a POST endpoint; a plain form keeps it working without JS state.
  return (
    <form method="post" action={logoutPath} className="m-0">
      <button
        type="submit"
        className="inline-flex h-[30px] items-center rounded-[3px] bg-raised px-3 text-xs font-semibold hover:bg-press active:translate-y-px"
      >
        Logout
      </button>
    </form>
  )
}

/// <reference types="vite/client" />

declare global {
  interface Window {
    /** Base path injected by the server (e.g. "/stemtrace"). */
    __STEMTRACE_BASE__?: string
    /** Logout endpoint path, injected only when built-in form login is enabled. */
    __STEMTRACE_LOGOUT_PATH__?: string
  }
}

export {}

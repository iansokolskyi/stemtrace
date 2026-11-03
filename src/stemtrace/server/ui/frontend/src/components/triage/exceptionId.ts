/** The id of the failures that carry no exception class (the API's `exception_type: null`). */
export const NO_EXCEPTION_ID = '(no exception class)'

/** The `exception` entry id for an API exception class, null included. */
export const exceptionId = (exceptionType: string | null): string =>
  exceptionType ?? NO_EXCEPTION_ID

/** The exception class an entry id names; null for the no-class group. */
export const exceptionOfId = (id: string): string | null => (id === NO_EXCEPTION_ID ? null : id)

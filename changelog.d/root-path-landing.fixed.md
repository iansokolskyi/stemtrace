- `stemtrace server --root-path` no longer shows "Not Found" when you open the
  prefix itself (for example `https://example.com/monitoring/`). The server now
  redirects it, and any other page URL under the prefix such as a task link, to
  the dashboard at `<prefix>/stemtrace/`. See
  [Behind a reverse proxy](https://github.com/iansokolskyi/stemtrace/blob/main/docs/deployment.md#behind-a-reverse-proxy-with-a-path-prefix).

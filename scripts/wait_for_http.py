"""Wait for a local HTTP endpoint to respond with a 2xx status.

Used by the E2E Makefile targets to wait for the Docker Compose stack. Only
loopback hosts and the E2E Compose server service are accepted, so the script
cannot be pointed at arbitrary internal or external network targets.
"""

from __future__ import annotations

import argparse
import http.client
import time
import urllib.request
from urllib.parse import urlparse

ALLOWED_SCHEMES = frozenset({"http", "https"})
ALLOWED_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "server"})


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="health URL on a loopback host or E2E service")
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--interval", type=float, default=2.0)
    return parser.parse_args()


def main() -> None:
    """Wait for a local HTTP endpoint to become healthy (2xx) or exit non-zero."""
    args = _parse_args()
    url: str = args.url
    parsed = urlparse(url)
    if parsed.scheme in ALLOWED_SCHEMES and parsed.hostname in ALLOWED_HOSTS:
        deadline = time.monotonic() + args.timeout
        last_error: str | None = None

        while time.monotonic() < deadline:
            try:
                request = urllib.request.Request(url, method="GET")
                with urllib.request.urlopen(request, timeout=2.0) as response:
                    if 200 <= response.status < 300:
                        return
                    last_error = f"HTTP {response.status}"
            except (OSError, http.client.HTTPException) as exc:
                # URLError, timeouts and connection resets while the stack starts
                last_error = str(exc)

            time.sleep(args.interval)

        raise SystemExit(f"Service not healthy within {args.timeout}s: {last_error}")

    allowed = ", ".join(sorted(ALLOWED_HOSTS))
    raise SystemExit(
        f"Refusing to poll {url!r}: only http(s) URLs on {allowed} are allowed"
    )


if __name__ == "__main__":
    main()

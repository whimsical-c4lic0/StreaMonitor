"""Container health probe for the Streamonitor web interface."""

from __future__ import annotations

import os
import sys
import urllib.request


def main() -> int:
    port = os.environ.get("STRMNTR_PORT", "5000")
    url = f"http://127.0.0.1:{port}/health"
    try:
        # Ignore proxy environment variables: the probe is always local.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(url, timeout=3) as response:
            return 0 if response.status == 200 else 1
    except Exception as exc:  # pragma: no cover - depends on the running container
        print(f"healthcheck failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

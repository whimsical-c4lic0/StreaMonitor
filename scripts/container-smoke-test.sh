#!/usr/bin/env bash
# Exercise a built image the way a deployment would: start it with synthetic
# configuration, read the health endpoint, verify the runtime user can write
# persisted data, and require a clean SIGTERM shutdown.
set -euo pipefail

IMAGE="${1:-streamonitor:smoke}"
PORT="${PORT:-$(python3 - <<'PY'
import socket

sock = socket.socket()
sock.bind(("127.0.0.1", 0))
print(sock.getsockname()[1])
sock.close()
PY
)}"

tmpdir="$(mktemp -d)"
container="streamonitor-smoke-$$"
cleanup() {
    docker rm -f "$container" >/dev/null 2>&1 || true
    rm -rf "$tmpdir"
}
trap cleanup EXIT

python3 - "$tmpdir/config.json" "$tmpdir" <<'PY'
import json
import os
import sys

config, directory = sys.argv[1], sys.argv[2]
with open(config, "w", encoding="utf-8") as handle:
    json.dump([], handle)
os.chmod(config, 0o666)
downloads = os.path.join(directory, "downloads")
os.makedirs(downloads, exist_ok=True)
os.chmod(downloads, 0o777)
PY

docker run --detach --name "$container" \
    --publish "127.0.0.1:${PORT}:5000" \
    --env STRMNTR_HOST=0.0.0.0 \
    --env STRMNTR_PASSWORD= \
    --env STRMNTR_STATUS_FREQ=60 \
    --env STRMNTR_LIST_FREQ=60 \
    --volume "$tmpdir/config.json:/app/config.json" \
    --volume "$tmpdir/downloads:/app/downloads" \
    "$IMAGE" >/dev/null

health=""
for _ in $(seq 1 60); do
    if health="$(curl --fail --silent --show-error "http://127.0.0.1:${PORT}/health" 2>/dev/null)"; then
        break
    fi
    if ! docker inspect --format '{{.State.Running}}' "$container" | grep -q true; then
        docker logs "$container" >&2 || true
        echo "container exited before becoming healthy" >&2
        exit 1
    fi
    sleep 1
done

if [ -z "$health" ]; then
    docker logs "$container" >&2 || true
    echo "container did not become healthy" >&2
    exit 1
fi

python3 - "$health" <<'PY'
import json
import sys

payload = json.loads(sys.argv[1])
assert payload["status"] == "ok", payload
print("health:", payload)
PY

uid="$(docker exec "$container" id -u)"
if [ "$uid" = "0" ]; then
    echo "container is running as root" >&2
    exit 1
fi
echo "runtime uid: $uid"

docker exec "$container" sh -c 'test -w /app/config.json && test -w /app/downloads'
echo "persisted paths are writable"

start="$(date +%s)"
docker stop --time 30 "$container" >/dev/null
elapsed=$(( $(date +%s) - start ))
code="$(docker inspect --format '{{.State.ExitCode}}' "$container")"
if [ "$code" -ne 0 ] && [ "$code" -ne 143 ]; then
    docker logs "$container" >&2 || true
    echo "container exited with $code after ${elapsed}s" >&2
    exit 1
fi
echo "graceful shutdown in ${elapsed}s (exit $code)"

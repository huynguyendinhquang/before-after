#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
CONTAINER=${DEMO_POSTGRES_CONTAINER:-before-after-demo-postgres}
PORT=${DEMO_POSTGRES_PORT:-55440}
VENV=${DEMO_VENV:-$ROOT/.venv}
MEDIA_ROOT=${DEMO_MEDIA_ROOT:-/tmp/before-after-demo-media}

if [[ ${APP_ENV:-development} != development ]]; then
    echo "run-demo: APP_ENV must be development" >&2
    exit 2
fi
if [[ ${HOST:-127.0.0.1} != 127.0.0.1 && ${HOST:-127.0.0.1} != localhost ]]; then
    echo "run-demo: demo server may bind only to localhost" >&2
    exit 2
fi
command -v docker >/dev/null || { echo "run-demo: docker is required" >&2; exit 2; }

if ! docker inspect "$CONTAINER" >/dev/null 2>&1; then
    docker run -d --name "$CONTAINER" \
        -e POSTGRES_PASSWORD=demo \
        -e POSTGRES_DB=before_after_demo \
        -p "127.0.0.1:${PORT}:5432" \
        postgres:16-alpine >/dev/null
elif [[ $(docker inspect -f '{{.State.Running}}' "$CONTAINER") != true ]]; then
    docker start "$CONTAINER" >/dev/null
fi

for _ in $(seq 1 30); do
    if docker exec "$CONTAINER" pg_isready -U postgres -d before_after_demo >/dev/null 2>&1; then
        break
    fi
    sleep 1
done
docker exec "$CONTAINER" pg_isready -U postgres -d before_after_demo >/dev/null

if [[ ! -x "$VENV/bin/python" ]]; then
    python3 -m venv "$VENV"
fi
if ! "$VENV/bin/python" -c 'import flask, flask_sqlalchemy, PIL, psycopg' >/dev/null 2>&1; then
    "$VENV/bin/pip" install -q -r "$ROOT/requirements.txt"
fi

install -d -m 0700 "$MEDIA_ROOT"
export APP_ENV=development
export DATABASE_URL="postgresql+psycopg://postgres:demo@127.0.0.1:${PORT}/before_after_demo"
export MEDIA_ROOT
export SECRET_KEY=local-demo-only-not-for-production
export DEMO_AUTO_LOGIN=1
export DEMO_USERNAME=demo
export HOST=127.0.0.1
export PORT=${DEMO_APP_PORT:-8765}

cd "$ROOT"
"$VENV/bin/alembic" upgrade head
"$VENV/bin/flask" --app app:create_app seed-demo

echo
echo "Before/After demo: http://${HOST}:${PORT}/"
echo "Auto-login is enabled. Fallback credentials: demo / demo"
echo "Press Ctrl-C to stop the web server; PostgreSQL data is kept for the next run."
echo
exec "$VENV/bin/python" -m app

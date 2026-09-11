#!/usr/bin/env sh
# Apply migrations before serving. The db service is already healthy by the
# time this runs (compose `depends_on: condition: service_healthy`), so no
# wait-for-it loop is needed.
set -e

echo "Applying database migrations..."
alembic upgrade head

echo "Starting API..."
exec "$@"

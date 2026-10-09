#!/usr/bin/env sh
# Apply migrations before serving. The db service is already healthy by the
# time this runs (compose `depends_on: condition: service_healthy`), so no
# wait-for-it loop is needed.
set -e

# SKIP_MIGRATIONS lets a second container (the worker) share this image without
# racing the API container to apply the same migrations.
if [ -z "$SKIP_MIGRATIONS" ]; then
  echo "Applying database migrations..."
  alembic upgrade head
fi

echo "Starting: $*"
exec "$@"

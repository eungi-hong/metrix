#!/usr/bin/env sh
# Apply migrations before serving. The db service is already healthy by the
# time this runs (compose `depends_on: condition: service_healthy`), so no
# wait-for-it loop is needed.
set -e

# SKIP_MIGRATIONS lets a second container (the worker) share this image without
# racing the API container to apply the same migrations.
#
# In production (Railway) both services set SKIP_MIGRATIONS=1, and the API
# service's pre-deploy command, `alembic upgrade head`, migrates once per
# deploy before any new container starts. Migrating here instead would run
# in every replica and on every restart, and a failed migration would
# crash-loop the service rather than stop the deploy. See docs/DEPLOYMENT.md.
if [ -z "$SKIP_MIGRATIONS" ]; then
  echo "Applying database migrations..."
  alembic upgrade head
fi

echo "Starting: $*"
exec "$@"

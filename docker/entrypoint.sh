#!/bin/sh
# On first start, write the settings the compose file chose into /data/.env, where the web
# UI can change them later. Nothing here overrides what you have saved since.
set -e
if [ ! -f /data/.env ]; then
  umask 077
  {
    echo "# Written on first start. Change these in the web UI's settings."
    echo "LEFT_PROVIDER=${SEED_LEFT_PROVIDER:-ollama}"
    echo "LEFT_MODEL=${SEED_LEFT_MODEL:-tev1}"
    echo "LEFT_BASE_URL=${SEED_OLLAMA_URL:-http://ollama:11434}"
    if [ -n "${SEED_RIGHT_MODEL:-}" ]; then
      echo "RIGHT_PROVIDER=${SEED_RIGHT_PROVIDER:-ollama}"
      echo "RIGHT_MODEL=${SEED_RIGHT_MODEL}"
      [ "${SEED_RIGHT_PROVIDER:-ollama}" = "ollama" ] && echo "RIGHT_BASE_URL=${SEED_OLLAMA_URL:-http://ollama:11434}/v1"
    fi
  } > /data/.env
fi
if [ "$#" -gt 0 ]; then
  exec hemisphere-harness "$@"
fi
exec hemisphere-harness web --host 0.0.0.0 --port 8765 --no-open --env-file /data/.env --data-dir /data/hemisphere

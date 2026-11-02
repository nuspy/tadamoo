#!/bin/bash
set -e

# All configuration comes from the environment the benchmark sets (from models/models.yaml): BASE_URL and MODEL_NAME.
if [ -z "$BASE_URL" ] || [ -z "$MODEL_NAME" ]; then
  echo "ERROR: BASE_URL and MODEL_NAME must be set"
  exit 1
fi
if [ -n "$TEMPERATURE" ]; then
  echo "WARN: the Basalt harness uses the sampling of the model server; TEMPERATURE='$TEMPERATURE' is ignored."
fi

# The harness must import before the browser time is spent on it.
PYTHONPATH=/opt/basalt python3 -c "import tadamoo.headless, tadamoo.bench.adapters.containerkit" || {
  echo "ERROR: the Basalt harness does not import"
  exit 1
}
echo "Basalt harness ready (model=${MODEL_NAME}, server=${BASE_URL})"

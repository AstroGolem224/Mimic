#!/bin/sh
set -eu

cd "$(dirname "$0")/.."

# Mit den Projektabhaengigkeiten, nicht mit --no-project: seit den Effekten
# zieht mimic.effekte numpy und scipy, und ohne sie scheitern 98 Tests schon
# am Import. Die Suite war damit ueber den offiziellen Weg nie gruen.
if [ "${1-}" = "--gpu" ]; then
    MIMIC_GPU_TESTS=1 uv run python -m unittest -v tests.test_phase1 tests.test_phase2 tests.test_ansage tests.test_entwurf tests.test_gpu_containment
else
    uv run python -m unittest -v tests.test_phase1 tests.test_phase2 tests.test_ansage tests.test_entwurf
fi

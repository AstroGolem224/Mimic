#!/bin/sh
set -eu

cd "$(dirname "$0")/.."

if [ "${1-}" = "--gpu" ]; then
    MIMIC_GPU_TESTS=1 uv run --no-project --python 3.12 python -m unittest -v tests.test_phase1 tests.test_phase2 tests.test_ansage tests.test_gpu_containment
else
    uv run --no-project --python 3.12 python -m unittest -v tests.test_phase1 tests.test_phase2 tests.test_ansage
fi

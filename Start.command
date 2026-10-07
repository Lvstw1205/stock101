#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then bash scripts/setup.sh; fi
exec .venv/bin/python run.py

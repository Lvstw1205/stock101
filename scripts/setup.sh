#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
python3 -c 'import sys; assert sys.version_info >= (3,11), "Python 3.11+ is required"'
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
printf '\nSetup complete. Run: .venv/bin/python run.py\n'

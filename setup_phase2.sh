#!/usr/bin/env sh
set -e
python -m pip install -r requirements.txt
python -m playwright install chromium
echo "Phase 2 browser runtime installed successfully."

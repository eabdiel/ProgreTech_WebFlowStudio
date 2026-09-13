#!/usr/bin/env sh
set -e
python -m pip install -r requirements.txt
python -m playwright install chromium
echo "Phase 6 dependencies installed. Run: python main.py"

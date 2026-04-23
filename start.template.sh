#!/bin/bash
set -euo pipefail

# Copy this file to start.sh after deployment and replace the example paths.
exec /path/to/venv/bin/uwsgi \
  --ini /path/to/bilibili_danmaku/uwsgi.ini

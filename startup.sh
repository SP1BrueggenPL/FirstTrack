#!/bin/bash
# Azure App Service startup command for FirstTrack.
# Playwright needs Chromium's browser binary, which isn't installed by
# `pip install` alone. Cache it under /home (persistent across restarts)
# so it's only downloaded once instead of on every cold start.
export PLAYWRIGHT_BROWSERS_PATH=/home/.cache/ms-playwright

if [ ! -d "$PLAYWRIGHT_BROWSERS_PATH" ]; then
    playwright install --with-deps chromium
fi

gunicorn firsttrack.wsgi --bind=0.0.0.0:8000

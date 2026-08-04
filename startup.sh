#!/bin/bash
# Azure App Service startup command for FirstTrack.
# Playwright needs Chromium's browser binary, which isn't installed by
# `pip install` alone. Cache it under /home (persistent across restarts)
# so it's only downloaded once instead of on every cold start.
export PLAYWRIGHT_BROWSERS_PATH=/home/.cache/ms-playwright

# Always run the installer instead of gating on `[ -d "$PLAYWRIGHT_BROWSERS_PATH" ]`:
# Playwright's own installer already skips the download when the correct
# browser version is already cached, so this stays fast on warm starts. The
# old directory-only check could not tell a complete cache from a partial one
# left by an interrupted previous install (e.g. the app restarting mid-
# download, or a transient network error) - once the directory existed the
# install was skipped forever, leaving PDF generation permanently broken
# ("PDF nie drukuje się"). `--with-deps` needs apt + root, which isn't always
# available on App Service, so fall back to a plain browser-only install if
# that fails rather than leaving Chromium missing entirely.
if ! playwright install --with-deps chromium; then
    echo "WARNING: 'playwright install --with-deps chromium' failed, retrying without --with-deps..." >&2
    if ! playwright install chromium; then
        echo "WARNING: playwright install chromium failed - PDF generation will not work until this is fixed." >&2
    fi
fi

gunicorn firsttrack.wsgi --bind=0.0.0.0:8000

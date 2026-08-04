#!/bin/bash
# Azure App Service startup command for FirstTrack.
#
# PDF generation used to rely on Playwright + a separately downloaded
# Chromium binary, which needed a ~300MB download (and often apt/root access
# for --with-deps) on every fresh instance. That download/install step kept
# failing or getting stuck on App Service, permanently breaking PDF export
# ("PDF nie drukuje się"). PDFs are now rendered with WeasyPrint, a pure-
# Python renderer installed via requirements.txt like any other dependency -
# no separate browser download or apt step needed here anymore.

gunicorn firsttrack.wsgi --bind=0.0.0.0:8000

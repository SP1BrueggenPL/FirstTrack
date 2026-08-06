#!/bin/bash
# Azure App Service startup command for FirstTrack.
#
# PDF generation used to rely on Playwright + a separately downloaded
# Chromium binary, which needed a ~300MB download (and often apt/root access
# for --with-deps) on every fresh instance. That download/install step kept
# failing or getting stuck on App Service, permanently breaking PDF export
# ("PDF nie drukuje się"). PDFs are now rendered with WeasyPrint, a
# mostly-Python renderer installed via requirements.txt like any other
# dependency - but it still needs a few native system libraries (Pango/
# cairo/GDK-PixBuf) that pip can't install. The App Service Python base image
# doesn't ship them, so WeasyPrint kept failing with "biblioteka WeasyPrint
# (lub jej zależność systemowa) nie jest zainstalowana". The container's
# filesystem outside /home isn't persisted between restarts, so this has to
# run on every startup, not just once.
if command -v apt-get >/dev/null 2>&1; then
  apt-get update -qq && apt-get install -y --no-install-recommends \
    libpango-1.0-0 libpangoft2-1.0-0 libpangocairo-1.0-0 \
    libgdk-pixbuf2.0-0 libcairo2 shared-mime-info \
    > /tmp/weasyprint-apt-install.log 2>&1
  if [ $? -ne 0 ]; then
    echo "WARNING: apt-get install dla zależności WeasyPrint nie powiodło się - patrz /tmp/weasyprint-apt-install.log. Generowanie PDF może nie działać."
  fi
fi

# Apply any pending Django migrations before serving traffic. This step was
# missing before, so the production database schema silently drifted from
# what the code expects (e.g. it kept a NOT NULL "phone" column on
# UserProfile long after that field was removed from the model in migration
# 0008), causing IntegrityError/500s on operations the local dev DB - which
# always runs migrate - never showed.
python manage.py migrate --noinput

# Bez --workers gunicorn startuje z 1 workerem, czyli cała aplikacja jest w
# praktyce jednowątkowa - jedno wolniejsze żądanie (np. duży import Excela,
# generowanie PDF) blokuje kolejkę dla wszystkich innych użytkowników, którzy
# po 30s (domyślny --timeout) dostają 500 mimo że ich strona nie ma nic
# wspólnego z tym, co faktycznie się wykonuje. To był rzeczywisty powód
# zgłoszeń "500 praktycznie wszędzie" widocznych w Log Streamie jako
# "WORKER TIMEOUT" / "SIGKILL".
gunicorn firsttrack.wsgi --bind=0.0.0.0:8000 --workers 3 --timeout 60

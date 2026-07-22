# FirstTrack – Nadzorowanie Pierwszej Produkcji
H. & J. Brüggen KG | CD-00002055

## Uruchomienie

```bash
pip install -r requirements.txt
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver
```

Aplikacja dostępna pod: http://127.0.0.1:8000/
Panel admina:          http://127.0.0.1:8000/admin/

## Klucz API (ekstrakcja AI z SAP)

Ustaw zmienną środowiskową przed uruchomieniem:

```bash
# Windows PowerShell
$env:ANTHROPIC_API_KEY = "sk-ant-..."
python manage.py runserver
```

## Email (SMTP)

W `firsttrack/settings.py` zmień backend z `filebased` na SMTP:

```python
EMAIL_BACKEND = 'django.core.mail.backends.smtp.EmailBackend'
EMAIL_HOST = 'smtp.brueggen.com'
EMAIL_PORT = 587
EMAIL_USE_TLS = True
EMAIL_HOST_USER = 'user@brueggen.com'
EMAIL_HOST_PASSWORD = '...'
```

## Proces (3 etapy)

1. **Import z SAP** – wgraj screenshot tabeli → AI wyciąga dane → twórz rekordy
2. **Etap I (Spotkanie)** – checklista *przed* produkcją, podpisy działów, mail do akceptującego
3. **Etap II (Linia)** – checklista *po* produkcji: parametry sensoryczne + opakowanie
4. **Etap III** – zdjęcia, UMK do śluzy, zwolnienie do sprzedaży

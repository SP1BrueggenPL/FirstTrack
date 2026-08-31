import base64
import logging

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.template.loader import render_to_string

from .models import FirstProduction

logger = logging.getLogger(__name__)


class PdfGenerationError(Exception):
    """Podnoszony, gdy PDF nie mógł zostać wygenerowany.
    Importujemy WeasyPrint leniwie (dopiero tutaj, nie na poziomie modułu),
    żeby brak tej biblioteki nie wywalał całej aplikacji przy starcie, tylko
    konkretną akcję generowania PDF."""


def _render_pdf(template_name, context):
    """Renderuje szablon do PDF przez WeasyPrint - czysty Python, bez potrzeby
    instalowania i utrzymywania przeglądarki (Chromium) na serwerze. Rozmiar
    strony i marginesy są ustawiane przez CSS `@page` w samych szablonach."""
    html = render_to_string(template_name, context)
    try:
        from weasyprint import HTML
    except (ImportError, OSError) as e:
        # ImportError: pakiet weasyprint nie jest zainstalowany.
        # OSError: pakiet jest, ale brakuje natywnej biblioteki systemowej,
        # od której zależy (Pango/cairo/GObject) - dokładnie to zdarzyło się
        # na Azure App Service (libgobject-2.0-0 nie było na obrazie).
        logger.error('WeasyPrint niedostępny - nie można wygenerować PDF: %s', e)
        raise PdfGenerationError(
            'Nie udało się wygenerować PDF: biblioteka WeasyPrint (lub jej zależność systemowa) '
            'nie jest zainstalowana na serwerze. Skontaktuj się z administratorem systemu.'
        ) from e

    try:
        return HTML(string=html).write_pdf()
    except Exception as e:
        logger.error('Błąd generowania PDF (WeasyPrint): %s', e, exc_info=True)
        raise PdfGenerationError(
            'Nie udało się wygenerować PDF z powodu błędu renderowania. Skontaktuj się z administratorem.'
        ) from e


# 8 ról z odręcznym podpisem (canvas, base64 PNG już gotowy jako src
# obrazka) - Sprzedaż Lubeck dokumentuje obecność zdjęciem, obsługiwana
# osobno (_build_sl_photo/_build_sl_photo_attachment).
SIGNATURE_FIELDS = [
    ('R&D', 'sig_rd', 'person_rd'),
    ('SC',  'sig_sc', 'person_sc'),
    ('QL',  'sig_ql', 'person_ql'),
    ('QA',  'sig_qa', 'person_qa'),
    ('SD',  'sig_sd', 'person_sd'),
    ('PP',  'sig_pp', 'person_pp'),
    ('CE',  'sig_ce', 'person_ce'),
    ('PT',  'sig_te', 'person_te'),
]


def _photo_data_uri(image_field):
    """Zdjęcie jako inline data: URI, nie ścieżka /media/... - WeasyPrint
    generuje PDF bez serwera HTTP, który mógłby taką ścieżkę obsłużyć, więc
    obraz trzeba wbudować bezpośrednio w wygenerowany HTML."""
    if not image_field:
        return None
    try:
        with image_field.open('rb') as f:
            data = f.read()
    except Exception as e:
        logger.warning('Nie udało się wczytać zdjęcia %s do PDF: %s', image_field.name, e)
        return None
    ext = image_field.name.rsplit('.', 1)[-1].lower()
    mime = {'jpg': 'image/jpeg', 'jpeg': 'image/jpeg', 'png': 'image/png',
            'gif': 'image/gif', 'webp': 'image/webp'}.get(ext, 'image/jpeg')
    return f'data:{mime};base64,{base64.b64encode(data).decode("ascii")}'


def _checklist_photo_uris(ca):
    if not ca:
        return []
    uris = [_photo_data_uri(getattr(ca, field)) for field in ('photo_1', 'photo_2', 'photo_3', 'photo_4')]
    return [uri for uri in uris if uri]


def _photo_attachment(image_field):
    """Zdjęcie jako (nazwa, bajty, mimetype) do doczepienia do maila -
    osobno od PDF, bo w mailu chcemy zdjęcia w oryginalnej rozdzielczości,
    nie zmniejszone przez layout PDF-a."""
    if not image_field:
        return None
    try:
        with image_field.open('rb') as f:
            data = f.read()
    except Exception as e:
        logger.warning('Nie udało się wczytać zdjęcia %s do maila: %s', image_field.name, e)
        return None
    ext = image_field.name.rsplit('.', 1)[-1].lower()
    mime = {'jpg': 'image/jpeg', 'jpeg': 'image/jpeg', 'png': 'image/png',
            'gif': 'image/gif', 'webp': 'image/webp'}.get(ext, 'image/jpeg')
    filename = image_field.name.rsplit('/', 1)[-1]
    return (filename, data, mime)


def _checklist_photo_attachments(ca):
    if not ca:
        return []
    atts = [_photo_attachment(getattr(ca, field)) for field in ('photo_1', 'photo_2', 'photo_3', 'photo_4')]
    return [a for a in atts if a]


def _build_team_signatures(prod, ca):
    """Odręczne podpisy (canvas) obecnych członków zespołu jako (rola, imię
    i nazwisko, base64 PNG gotowy jako src obrazka) - tylko dla osób
    faktycznie przypisanych do produkcji i tylko gdy podpis został złożony."""
    if not ca:
        return []
    sigs = []
    for role, sig_field, person_field in SIGNATURE_FIELDS:
        person = getattr(prod, person_field)
        sig = getattr(ca, sig_field, '')
        if person and sig:
            sigs.append((role, person.get_full_name(), sig))
    return sigs


def _build_sl_photo(prod, ca):
    """Sprzedaż Lubeck dokumentuje obecność zdjęciem, nie podpisem - zwraca
    (imię i nazwisko, data URI) albo None, gdy nie ma przypisanej osoby albo
    zdjęcia."""
    if not ca or not prod.person_sl:
        return None
    uri = _photo_data_uri(ca.photo_sl)
    if not uri:
        return None
    return (prod.person_sl.get_full_name(), uri)


def _build_sl_photo_attachment(prod, ca):
    """To samo co _build_sl_photo, ale jako (nazwa, bajty, mimetype) do
    doczepienia do maila w oryginalnej rozdzielczości."""
    if not ca or not prod.person_sl:
        return None
    return _photo_attachment(ca.photo_sl)


def _linked_checklist_data(prod):
    """Powiązana para sensoryka/pakowanie jest w praktyce jedną produkcją -
    parametry sensoryczne i pozycje pakowania fizycznie żyją na tej z dwóch
    produkcji, która faktycznie ją wykonuje. PDF-y (Etap II/III) muszą więc
    złożyć dane z obu stron, a nie tylko z tej, dla której PDF jest
    generowany - inaczej strona pakowania nie widziała danych sensorycznych
    (i odwrotnie)."""
    sensory_prod, packaging_prod = prod, prod
    if prod.linked_production:
        if prod.is_sensory_only:
            packaging_prod = prod.linked_production
        elif prod.is_packaging_only:
            sensory_prod = prod.linked_production

    sensory_ca   = getattr(sensory_prod, 'checklist_after', None)
    packaging_ca = getattr(packaging_prod, 'checklist_after', None)

    team_signatures = _build_team_signatures(sensory_prod, sensory_ca)
    sl_photo = _build_sl_photo(sensory_prod, sensory_ca)
    sl_photo_attachment = _build_sl_photo_attachment(sensory_prod, sensory_ca)
    photo_uris = _checklist_photo_uris(sensory_ca)
    photo_attachments = _checklist_photo_attachments(sensory_ca)
    if packaging_prod.pk != sensory_prod.pk:
        team_signatures = team_signatures + _build_team_signatures(packaging_prod, packaging_ca)
        if not sl_photo:
            sl_photo = _build_sl_photo(packaging_prod, packaging_ca)
            sl_photo_attachment = _build_sl_photo_attachment(packaging_prod, packaging_ca)
        photo_uris = photo_uris + _checklist_photo_uris(packaging_ca)
        photo_attachments = photo_attachments + _checklist_photo_attachments(packaging_ca)

    return {
        'sensory': sensory_ca.sensory_params.all() if sensory_ca else [],
        'packaging': packaging_ca.packaging_items.all() if packaging_ca else [],
        'team_signatures': team_signatures,
        'sl_photo': sl_photo,
        'sl_photo_attachment': sl_photo_attachment,
        'photo_uris': photo_uris,
        'photo_attachments': photo_attachments,
    }


def _generate_pdf_etap3(prod, ca):
    """Używane też przy wysyłce maila ze zwolnieniem (załącznik PDF)."""
    cb = getattr(prod, 'checklist_before', None)
    return _render_pdf('productions/pdf/etap3.html', {
        'production': prod, 'cb': cb, 'ca': ca,
        **_linked_checklist_data(prod),
    })


@login_required
def pdf_etap1(request, pk):
    prod = get_object_or_404(FirstProduction, pk=pk)
    cb = getattr(prod, 'checklist_before', None)
    context = {'production': prod, 'cb': cb}
    try:
        pdf = _render_pdf('productions/pdf/etap1.html', context)
    except PdfGenerationError as e:
        messages.error(request, str(e))
        return redirect('production_detail', pk=pk)
    response = HttpResponse(pdf, content_type='application/pdf')
    response['Content-Disposition'] = f'attachment; filename="PP_{prod.sap_zlecenie}_Etap1.pdf"'
    return response


@login_required
def pdf_etap2(request, pk):
    prod = get_object_or_404(FirstProduction, pk=pk)
    ca = getattr(prod, 'checklist_after', None)
    context = {
        'production': prod, 'ca': ca,
        **_linked_checklist_data(prod),
    }
    try:
        pdf = _render_pdf('productions/pdf/etap2.html', context)
    except PdfGenerationError as e:
        messages.error(request, str(e))
        return redirect('production_detail', pk=pk)
    response = HttpResponse(pdf, content_type='application/pdf')
    response['Content-Disposition'] = f'attachment; filename="PP_{prod.sap_zlecenie}_Etap2.pdf"'
    return response


@login_required
def pdf_etap3(request, pk):
    prod = get_object_or_404(FirstProduction, pk=pk)
    ca = getattr(prod, 'checklist_after', None)
    try:
        if ca:
            pdf = _generate_pdf_etap3(prod, ca)
        else:
            cb = getattr(prod, 'checklist_before', None)
            pdf = _render_pdf('productions/pdf/etap3.html', {
                'production': prod, 'cb': cb, 'ca': None,
                'sensory': [], 'packaging': [], 'team_signatures': [], 'sl_photo': None, 'photo_uris': [],
            })
    except PdfGenerationError as e:
        messages.error(request, str(e))
        return redirect('production_detail', pk=pk)
    response = HttpResponse(pdf, content_type='application/pdf')
    response['Content-Disposition'] = f'attachment; filename="PP_{prod.sap_zlecenie}_Zwolnienie.pdf"'
    return response

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


def _build_team_sigs(prod, ca):
    sigs = []
    for role, fname, person in [
        ('R&D', 'sig_rd', prod.person_rd),
        ('SC',  'sig_sc', prod.person_sc),
        ('QL',  'sig_ql', prod.person_ql),
        ('QA',  'sig_qa', prod.person_qa),
        ('SD',  'sig_sd', prod.person_sd),
        ('PP',  'sig_pp', prod.person_pp),
        ('CE',  'sig_ce', prod.person_ce),
        ('Technologia', 'sig_te', prod.person_te),
    ]:
        sig = getattr(ca, fname, '')
        if person and sig:
            sigs.append((role, person.get_full_name(), sig))
    return sigs


def _generate_pdf_etap3(prod, ca):
    """Używane też przy wysyłce maila ze zwolnieniem (załącznik PDF)."""
    cb = getattr(prod, 'checklist_before', None)
    return _render_pdf('productions/pdf/etap3.html', {
        'production': prod, 'cb': cb, 'ca': ca,
        'sensory': ca.sensory_params.all(),
        'packaging': ca.packaging_items.all(),
        'team_sigs': _build_team_sigs(prod, ca),
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
        'sensory': ca.sensory_params.all() if ca else [],
        'packaging': ca.packaging_items.all() if ca else [],
        'team_sigs': _build_team_sigs(prod, ca) if ca else [],
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
                'production': prod, 'cb': cb, 'ca': None, 'sensory': [], 'packaging': [], 'team_sigs': [],
            })
    except PdfGenerationError as e:
        messages.error(request, str(e))
        return redirect('production_detail', pk=pk)
    response = HttpResponse(pdf, content_type='application/pdf')
    response['Content-Disposition'] = f'attachment; filename="PP_{prod.sap_zlecenie}_Zwolnienie.pdf"'
    return response

import os
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.template.loader import render_to_string
from django.contrib.auth.decorators import login_required
from playwright.sync_api import sync_playwright

from .models import FirstProduction


def _render_pdf(template_name, context):
    html = render_to_string(template_name, context)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.set_content(html, wait_until='networkidle')
        pdf = page.pdf(
            format='A4',
            landscape=True,
            margin={'top': '10mm', 'bottom': '10mm', 'left': '10mm', 'right': '10mm'},
            print_background=True,
        )
        browser.close()
    return pdf


def _build_team_sigs(prod, ca):
    sigs = []
    for role, fname, person in [
        ('R&D', 'sig_rd', prod.person_rd),
        ('SC',  'sig_sc', prod.person_sc),
        ('QL',  'sig_ql', prod.person_ql),
        ('QA',  'sig_qa', prod.person_qa),
        ('SD',  'sig_sd', prod.person_sd),
        ('SDP', 'sig_sdp', prod.person_sdp),
        ('PP',  'sig_pp', prod.person_pp),
        ('CE',  'sig_ce', prod.person_ce),
    ]:
        sig = getattr(ca, fname, '')
        if person and sig:
            sigs.append((role, person.get_full_name(), sig))
    return sigs


@login_required
def pdf_etap1(request, pk):
    prod = get_object_or_404(FirstProduction, pk=pk)
    cb = getattr(prod, 'checklist_before', None)
    context = {'production': prod, 'cb': cb}
    pdf = _render_pdf('productions/pdf/etap1.html', context)
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
    pdf = _render_pdf('productions/pdf/etap2.html', context)
    response = HttpResponse(pdf, content_type='application/pdf')
    response['Content-Disposition'] = f'attachment; filename="PP_{prod.sap_zlecenie}_Etap2.pdf"'
    return response


@login_required
def pdf_etap3(request, pk):
    prod = get_object_or_404(FirstProduction, pk=pk)
    cb = getattr(prod, 'checklist_before', None)
    ca = getattr(prod, 'checklist_after', None)
    context = {
        'production': prod, 'cb': cb, 'ca': ca,
        'sensory': ca.sensory_params.all() if ca else [],
        'packaging': ca.packaging_items.all() if ca else [],
        'team_sigs': _build_team_sigs(prod, ca) if ca else [],
    }
    pdf = _render_pdf('productions/pdf/etap3.html', context)
    response = HttpResponse(pdf, content_type='application/pdf')
    response['Content-Disposition'] = f'attachment; filename="PP_{prod.sap_zlecenie}_Zwolnienie.pdf"'
    return response

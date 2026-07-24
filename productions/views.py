import json
import base64
import logging
from .pdf_views import pdf_etap1, pdf_etap2, pdf_etap3
from django.core.cache import cache
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib import messages
from django.contrib.auth import authenticate, login as auth_login
from django.contrib.auth.models import User
from django.http import JsonResponse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.core.mail import send_mail, EmailMessage
from django.conf import settings
from django.views.decorators.http import require_POST
from django.contrib.auth.decorators import login_required

from .models import (
    FirstProduction, ChecklistBefore, ChecklistAfter,
    EmailLog, UserProfile, NotificationRecipient, DEPT_CHOICES,
)
from .forms import (
    FirstProductionForm, SAPImportForm,
    ChecklistBeforeForm,
    ChecklistAfterSensoryForm, ChecklistAfterPackagingForm, ChecklistAfterAcceptanceForm,
    ChecklistAfterHeaderForm,
    SensoryParamFormSet, PackagingItemFormSet,
    UserCreateForm, UserEditForm, UserChipForm, NotificationRecipientForm,
)

logger = logging.getLogger(__name__)

CHIP_LOGIN_MAX_ATTEMPTS = 5
CHIP_LOGIN_LOCKOUT_SECONDS = 300


def _client_ip(request):
    forwarded = request.META.get('HTTP_X_FORWARDED_FOR', '')
    if forwarded:
        return forwarded.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR', 'unknown')


def chip_login(request):
    """Logowanie samym 5-cyfrowym numerem chip (bez loginu/hasła)."""
    if request.user.is_authenticated:
        return redirect('dashboard')

    cache_key = f'chip_login_fail_{_client_ip(request)}'
    locked_out = cache.get(cache_key, 0) >= CHIP_LOGIN_MAX_ATTEMPTS
    error = None
    next_url = request.POST.get('next') or request.GET.get('next') or ''

    if request.method == 'POST' and not locked_out:
        chip_number = request.POST.get('chip_number', '').strip()
        user = authenticate(request, chip_number=chip_number)
        if user is not None:
            cache.delete(cache_key)
            auth_login(request, user)
            if not url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}):
                next_url = ''
            return redirect(next_url or 'dashboard')
        cache.set(cache_key, cache.get(cache_key, 0) + 1, CHIP_LOGIN_LOCKOUT_SECONDS)
        error = True
        locked_out = cache.get(cache_key, 0) >= CHIP_LOGIN_MAX_ATTEMPTS

    return render(request, 'productions/login.html', {
        'error': error, 'locked_out': locked_out, 'next': next_url,
    })


# ──────────────────────────────────────────────
# Dashboard
# ──────────────────────────────────────────────

@login_required
def dashboard(request):
    _send_due_production_reminders()

    productions = FirstProduction.objects.select_related(
        'person_sd', 'acceptor'
    ).all()

    status_filter = request.GET.get('status', '')
    search = request.GET.get('q', '')

    if status_filter:
        productions = productions.filter(status=status_filter)
    if search:
        from django.db.models import Q
        productions = productions.filter(
            Q(product_name__icontains=search) |
            Q(sap_zlecenie__icontains=search) |
            Q(sap_material__icontains=search) |
            Q(layout__icontains=search) |
            Q(zmiany__icontains=search) |
            Q(typ_produkcji__icontains=search) |
            Q(person_sd__first_name__icontains=search) |
            Q(person_sd__last_name__icontains=search)
        )

    context = {
        'productions': productions,
        'status_choices': FirstProduction.STATUS_CHOICES,
        'status_filter': status_filter,
        'search': search,
        'counts': {
            'total':     FirstProduction.objects.count(),
            'nowa':      FirstProduction.objects.filter(status='nowa').count(),
            'etap1':     FirstProduction.objects.filter(status='etap1').count(),
            'etap2':     FirstProduction.objects.filter(status='etap2').count(),
            'etap3':     FirstProduction.objects.filter(status='etap3').count(),
            'zwolniona': FirstProduction.objects.filter(status='zwolniona').count(),
        },
    }
    return render(request, 'productions/dashboard.html', context)


# ──────────────────────────────────────────────
# Import SAP (AI ekstrakcja)
# ──────────────────────────────────────────────

@login_required
def import_sap(request):
    form = SAPImportForm()
    extracted = None

    if request.method == 'POST':
        form = SAPImportForm(request.POST, request.FILES)
        if form.is_valid():
            image_file = request.FILES['screenshot']
            image_data = image_file.read()
            image_b64  = base64.standard_b64encode(image_data).decode('utf-8')
            media_type = image_file.content_type or 'image/jpeg'

            extracted = _extract_sap_data(image_b64, media_type)
            if extracted:
                request.session['sap_extracted'] = extracted
                messages.success(request, f'AI wyciągnął dane: {len(extracted)} wierszy. Sprawdź i zatwierdź poniżej.')
            else:
                messages.error(request, 'Nie udało się wyciągnąć danych. Sprawdź klucz API lub spróbuj ręcznie.')

    prefill = request.session.pop('sap_extracted', None)
    return render(request, 'productions/import_sap.html', {
        'form': form,
        'extracted': prefill,
    })


def _extract_sap_data(image_b64: str, media_type: str) -> list | None:
    endpoint = getattr(settings, 'AZURE_OPENAI_ENDPOINT', '')
    api_key  = getattr(settings, 'AZURE_OPENAI_KEY', '')
    deployment = getattr(settings, 'AZURE_OPENAI_DEPLOYMENT', 'gpt-4o')

    if not api_key or not endpoint:
        logger.warning('AZURE_OPENAI_KEY / AZURE_OPENAI_ENDPOINT nie ustawione')
        return None

    try:
        from openai import AzureOpenAI
        client = AzureOpenAI(
            api_key=api_key,
            azure_endpoint=endpoint,
            api_version='2024-08-01-preview',
        )

        prompt = """Masz przed sobą screenshot tabeli z SAP lub tabeli planowania produkcji.

Wyciągnij WSZYSTKIE wiersze danych i zwróć je jako JSON array.
Każdy wiersz to obiekt z dokładnie tymi polami:
- sap_zlecenie    (numer zlecenia SAP, kolumna "Zlecenie" lub "Zlecenia", np. "11333525")
- sap_material    (numer materiału, kolumna "Materiał" lub "Materi...", np. "28124")
- product_name    (opis produktu, kolumna "Krótki tekst materiału" lub "Opis", np. "MSM Crunchy Pink 168x35g TC")
- data_produkcji  (data produkcji/rozp., kolumna "RozpWgH" lub "data produkcji", format YYYY-MM-DD, np. "2026-06-23")

Daty w formacie DD.MM.YYYY lub D.MM.YYYY zamień na YYYY-MM-DD.
Jeśli pole jest nieczytelne lub brak, użyj pustego stringa "".
Zwróć TYLKO JSON array, bez żadnego dodatkowego tekstu, komentarzy ani formatowania markdown."""

        response = client.chat.completions.create(
            model=deployment,
            max_tokens=2000,
            messages=[{
                'role': 'user',
                'content': [
                    {
                        'type': 'image_url',
                        'image_url': {'url': f'data:{media_type};base64,{image_b64}'},
                    },
                    {'type': 'text', 'text': prompt},
                ],
            }],
        )

        text = response.choices[0].message.content.strip()
        # Usuń ewentualne bloki markdown
        if '```' in text:
            parts = text.split('```')
            for part in parts:
                part = part.strip()
                if part.startswith('json'):
                    part = part[4:].strip()
                if part.startswith('['):
                    text = part
                    break
        return json.loads(text)

    except Exception as e:
        logger.error('Błąd ekstrakcji SAP (Azure OpenAI): %s', e, exc_info=True)
        return None


# ──────────────────────────────────────────────
# API – user email (do auto-fill akceptującego)
# ──────────────────────────────────────────────

@login_required
def api_user_email(request, pk):
    try:
        user = User.objects.get(pk=pk)
        return JsonResponse({'email': user.email, 'name': user.get_full_name()})
    except User.DoesNotExist:
        return JsonResponse({'email': '', 'name': ''})


# ──────────────────────────────────────────────
# API – prefill z SAP
# ──────────────────────────────────────────────

@login_required
@require_POST
def api_prefill_sap(request):
    try:
        rows = json.loads(request.body)
        created_productions = []
        for data in rows:
            date_val = data.get('data_produkcji') or None
            prod = FirstProduction.objects.create(
                sap_zlecenie=data.get('sap_zlecenie', ''),
                sap_material=data.get('sap_material', ''),
                product_name=data.get('product_name', '') or 'Nowa produkcja',
                data_produkcji=date_val,
            )
            created_productions.append(prod)
        _notify_new_productions(created_productions)
        return JsonResponse({'ok': True})
    except Exception as e:
        return JsonResponse({'ok': False, 'error': str(e)}, status=400)


# ──────────────────────────────────────────────
# Nowa / edycja produkcji
# ──────────────────────────────────────────────

@login_required
def production_new(request):
    prefill = request.session.pop('sap_prefill', None)
    initial = {}
    if prefill:
        # Mapuj pola z AI do pól formularza
        initial = {
            'sap_zlecenie':  prefill.get('sap_zlecenie', ''),
            'sap_material':  prefill.get('sap_material', ''),
            'product_name':  prefill.get('product_name', ''),
            'data_produkcji': prefill.get('data_produkcji', '') or None,
        }

    form = FirstProductionForm(initial=initial)
    if request.method == 'POST':
        form = FirstProductionForm(request.POST, request.FILES)
        if form.is_valid():
            prod = form.save()
            # Auto-fill email akceptującego jeśli nie wpisano
            if prod.acceptor and not prod.acceptor_email:
                prod.acceptor_email = prod.acceptor.email
                prod.save(update_fields=['acceptor_email'])
            _notify_new_productions([prod])
            messages.success(request, f'Produkcja „{prod.product_name}" została dodana.')
            return redirect('production_detail', pk=prod.pk)

    return render(request, 'productions/production_form.html', {
        'form': form, 'title': 'Nowa pierwsza produkcja',
    })


@login_required
def production_edit(request, pk):
    prod = get_object_or_404(FirstProduction, pk=pk)
    if prod.status == 'zwolniona':
        messages.error(request, 'Produkcja zwolniona do sprzedaży – edycja jest zablokowana.')
        return redirect('production_detail', pk=pk)
    form = FirstProductionForm(instance=prod)
    if request.method == 'POST':
        form = FirstProductionForm(request.POST, request.FILES, instance=prod)
        if form.is_valid():
            prod = form.save()
            if prod.acceptor and not prod.acceptor_email:
                prod.acceptor_email = prod.acceptor.email
                prod.save(update_fields=['acceptor_email'])
            messages.success(request, 'Dane produkcji zaktualizowane.')
            return redirect('production_detail', pk=prod.pk)
    return render(request, 'productions/production_form.html', {
        'form': form, 'production': prod,
        'title': f'Edytuj: {prod.product_name}',
    })


# ──────────────────────────────────────────────
# Szczegóły produkcji
# ──────────────────────────────────────────────

@login_required
def production_detail(request, pk):
    prod = get_object_or_404(
        FirstProduction.objects.select_related(
            'person_rd', 'person_sc', 'person_ql', 'person_qa',
            'person_sd', 'person_sdp', 'person_pp', 'person_ce', 'acceptor',
        ),
        pk=pk
    )

    if prod.status == 'zwolniona':
        edit_form = None
    elif request.method == 'POST' and 'save_basic' in request.POST:
        edit_form = FirstProductionForm(request.POST, instance=prod)
        if edit_form.is_valid():
            edit_form.save()
            messages.success(request, 'Dane produkcji zaktualizowane.')
            return redirect('production_detail', pk=pk)
    else:
        edit_form = FirstProductionForm(instance=prod)

    checklist_before = getattr(prod, 'checklist_before', None)
    checklist_after  = getattr(prod, 'checklist_after',  None)

    if checklist_after:
        cb = checklist_before
        update_fields = []
        if checklist_after.production_date is None and prod.data_produkcji:
            checklist_after.production_date = prod.data_produkcji
            update_fields.append('production_date')
        if not checklist_after.packaging_line and prod.packaging_line:
            checklist_after.packaging_line = prod.packaging_line
            update_fields.append('packaging_line')
        if cb:
            if not checklist_after.yield_kg and cb.planned_yield_kg:
                checklist_after.yield_kg = cb.planned_yield_kg
                update_fields.append('yield_kg')
            if not checklist_after.yield_takty and cb.planned_yield_takty:
                checklist_after.yield_takty = cb.planned_yield_takty
                update_fields.append('yield_takty')
        if update_fields:
            checklist_after.save(update_fields=update_fields)

    confirmations = []
    if checklist_before:
        confirmations = [
            ('R&D', checklist_before.confirm_rd),
            ('PP',  checklist_before.confirm_pp),
            ('CE',  checklist_before.confirm_ce),
            ('QA',  checklist_before.confirm_qa),
            ('SDP', checklist_before.confirm_sdp),
            ('SD',  checklist_before.confirm_sd),
        ]

    return render(request, 'productions/production_detail.html', {
        'production': prod,
        'edit_form': edit_form,
        'checklist_before': checklist_before,
        'checklist_after':  checklist_after,
        'checklist_confirmations': confirmations,
    })


# ──────────────────────────────────────────────
# Etap I – Checklista przed
# ──────────────────────────────────────────────

@login_required
def checklist_before(request, pk):
    prod     = get_object_or_404(FirstProduction, pk=pk)
    instance = getattr(prod, 'checklist_before', None)

    form = ChecklistBeforeForm(instance=instance)
    if request.method == 'POST':
        form = ChecklistBeforeForm(request.POST, instance=instance)
        if form.is_valid():
            cb = form.save(commit=False)
            cb.production = prod
            if 'complete' in request.POST:
                cb.completed_at = timezone.now()
                prod.status = 'etap1'
                prod.save()
            cb.save()
            messages.success(request, 'Checklista przed produkcją zapisana.')
            return redirect('production_detail', pk=pk)

    return render(request, 'productions/checklist_before.html', {
        'form': form, 'production': prod,
    })


# ──────────────────────────────────────────────
# Etap II – pomocnicza funkcja
# ──────────────────────────────────────────────

def _get_or_create_checklist_after(prod):
    cb = getattr(prod, 'checklist_before', None)
    instance = getattr(prod, 'checklist_after', None)
    if instance is None:
        instance = ChecklistAfter(
            production=prod,
            production_date=prod.data_produkcji,
            packaging_line=prod.packaging_line or '',
            yield_kg=cb.planned_yield_kg if cb else '',
            yield_takty=cb.planned_yield_takty if cb else '',
        )
        instance.save()
    else:
        update_fields = []
        if instance.production_date is None and prod.data_produkcji:
            instance.production_date = prod.data_produkcji
            update_fields.append('production_date')
        if not instance.packaging_line and prod.packaging_line:
            instance.packaging_line = prod.packaging_line
            update_fields.append('packaging_line')
        if cb:
            if not instance.yield_kg and cb.planned_yield_kg:
                instance.yield_kg = cb.planned_yield_kg
                update_fields.append('yield_kg')
            if not instance.yield_takty and cb.planned_yield_takty:
                instance.yield_takty = cb.planned_yield_takty
                update_fields.append('yield_takty')
        if update_fields:
            instance.save(update_fields=update_fields)
    return instance


def _all_sig_fields(prod):
    return [
        ('R&D', 'sig_rd',  prod.person_rd),
        ('SC',  'sig_sc',  prod.person_sc),
        ('QL',  'sig_ql',  prod.person_ql),
        ('QA',  'sig_qa',  prod.person_qa),
        ('SD',  'sig_sd',  prod.person_sd),
        ('SDP', 'sig_sdp', prod.person_sdp),
        ('PP',  'sig_pp',  prod.person_pp),
        ('CE',  'sig_ce',  prod.person_ce),
    ]


# Etap II krok 1 – sensoryczne
@login_required
def checklist_after(request, pk):
    return redirect('checklist_after_sensory', pk=pk)


@login_required
def checklist_after_sensory(request, pk):
    prod     = get_object_or_404(FirstProduction, pk=pk)
    instance = _get_or_create_checklist_after(prod)
    sensory_fs = SensoryParamFormSet(queryset=instance.sensory_params.all(), prefix='sensory')
    form = ChecklistAfterSensoryForm(instance=instance)

    if request.method == 'POST':
        form       = ChecklistAfterSensoryForm(request.POST, instance=instance)
        sensory_fs = SensoryParamFormSet(request.POST, queryset=instance.sensory_params.all(), prefix='sensory')
        if form.is_valid() and sensory_fs.is_valid():
            ca = form.save(commit=False)
            ca.production = prod
            ca.save()
            sensory_fs.save()
            messages.success(request, 'Parametry sensoryczne zapisane.')
            if 'next' in request.POST:
                _send_sensory_accepted_email(prod, ca)
                return redirect('checklist_after_packaging', pk=pk)
            return redirect('checklist_after_sensory', pk=pk)

    return render(request, 'productions/checklist_after_sensory.html', {
        'form': form,
        'sensory_fs': sensory_fs,
        'production': prod,
        'checklist': instance,
        'team_sig_fields': _all_sig_fields(prod),
        'step': 1,
    })


# Etap II krok 2 – pakowanie
@login_required
def checklist_after_packaging(request, pk):
    prod     = get_object_or_404(FirstProduction, pk=pk)
    instance = _get_or_create_checklist_after(prod)
    packaging_fs = PackagingItemFormSet(queryset=instance.packaging_items.all(), prefix='packaging')
    form = ChecklistAfterPackagingForm(instance=instance)

    all_sigs = _all_sig_fields(prod)
    unsigned = [(role, fname, person) for role, fname, person in all_sigs
                if person and not getattr(instance, fname)]

    if request.method == 'POST':
        form         = ChecklistAfterPackagingForm(request.POST, request.FILES, instance=instance)
        packaging_fs = PackagingItemFormSet(request.POST, queryset=instance.packaging_items.all(), prefix='packaging')
        if form.is_valid() and packaging_fs.is_valid():
            ca = form.save(commit=False)
            if 'complete' in request.POST:
                ca.completed_at = timezone.now()
                prod.status = 'etap2'
                prod.save()
            ca.save()
            packaging_fs.save()
            messages.success(request, 'Etap II zatwierdzony.' if 'complete' in request.POST else 'Checklista pakowania zapisana.')
            if 'complete' in request.POST:
                _send_packaging_accepted_email(prod)
                return redirect('production_detail', pk=pk)
            return redirect('checklist_after_packaging', pk=pk)

    return render(request, 'productions/checklist_after_packaging.html', {
        'form': form,
        'packaging_fs': packaging_fs,
        'production': prod,
        'checklist': instance,
        'unsigned_sig_fields': unsigned,
        'step': 2,
    })


# Etap II krok 3 – akceptacja SD
@login_required
def checklist_after_acceptance(request, pk):
    prod     = get_object_or_404(FirstProduction, pk=pk)
    instance = _get_or_create_checklist_after(prod)
    form = ChecklistAfterAcceptanceForm(instance=instance)

    if request.method == 'POST':
        form = ChecklistAfterAcceptanceForm(request.POST, instance=instance)
        if form.is_valid():
            ca = form.save(commit=False)
            ca.production = prod
            if 'complete' in request.POST:
                ca.completed_at = timezone.now()
                prod.status = 'etap2'
                prod.save()
            ca.save()
            messages.success(request, 'Etap II zatwierdzony.' if 'complete' in request.POST else 'Akceptacja zapisana.')
            return redirect('production_detail', pk=pk)

    return render(request, 'productions/checklist_after_acceptance.html', {
        'form': form,
        'production': prod,
        'checklist': instance,
        'step': 3,
    })


# ──────────────────────────────────────────────
# Etap III – Akceptacja SD + Zwolnienie
# ──────────────────────────────────────────────

@login_required
def release_production(request, pk):
    prod     = get_object_or_404(FirstProduction, pk=pk)
    instance = _get_or_create_checklist_after(prod)
    form = ChecklistAfterAcceptanceForm(instance=instance)

    if request.method == 'POST':
        form = ChecklistAfterAcceptanceForm(request.POST, request.FILES, instance=instance)
        if form.is_valid():
            ca = form.save(commit=False)
            ca.production = prod
            ca.save()
            if 'release' in request.POST:
                prod.status = 'zwolniona'
                prod.save()
                messages.success(request, f'Produkcja „{prod.product_name}" zwolniona do sprzedaży.')
                _send_release_email(prod, ca)
            else:
                messages.success(request, 'Akceptacja zapisana.')
            return redirect('production_detail', pk=pk)

    # podpisy zespołu do podglądu
    ca = instance
    signed = []
    for role, fname, person in _all_sig_fields(prod):
        if person and getattr(ca, fname):
            signed.append((role, fname, person, getattr(ca, fname)))

    return render(request, 'productions/checklist_after_acceptance.html', {
        'form': form,
        'production': prod,
        'checklist': instance,
        'signed_team': signed,
        'step': 3,
    })


@login_required
@require_POST
def production_delete(request, pk):
    if not request.user.is_staff:
        messages.error(request, 'Brak uprawnień do usunięcia rekordu.')
        return redirect('production_detail', pk=pk)
    prod = get_object_or_404(FirstProduction, pk=pk)
    name = prod.product_name
    prod.delete()
    messages.success(request, f'Produkcja „{name}" została usunięta.')
    return redirect('dashboard')


# ──────────────────────────────────────────────
# Email
# ──────────────────────────────────────────────

@login_required
@require_POST
def send_production_email(request, pk):
    prod = get_object_or_404(FirstProduction, pk=pk)

    subject = f'[FirstTrack] Pierwsza produkcja: {prod.product_name} – {prod.data_produkcji or "data TBD"}'
    body    = _build_email_body(prod)

    recipients = set()
    email = prod.acceptor_email or (prod.acceptor.email if prod.acceptor else '')
    if email:
        recipients.add(email)

    # Stała pula adresów – zawsze przy pierwszej produkcji (zarządzanie → adresy email)
    recipients.update(
        NotificationRecipient.objects.filter(active=True).values_list('email', flat=True)
    )

    # Reszta zespołu przypisanego do tej konkretnej produkcji
    team_fields = [
        'person_rd', 'person_sc', 'person_ql', 'person_qa',
        'person_sd', 'person_sdp', 'person_pp', 'person_ce',
    ]
    for field in team_fields:
        person = getattr(prod, field)
        if person and person.email:
            recipients.add(person.email)

    recipients = sorted(recipients)

    success = True
    error_msg = ''
    try:
        send_mail(subject=subject, message=body,
                  from_email=settings.DEFAULT_FROM_EMAIL,
                  recipient_list=recipients, fail_silently=False)
    except Exception as e:
        success = False
        error_msg = str(e)
        logger.error('Błąd wysyłki maila: %s', e)

    EmailLog.objects.create(
        production=prod, recipient=', '.join(recipients),
        subject=subject, body=body, success=success, error_msg=error_msg,
    )

    if success:
        prod.email_sent = True
        prod.email_sent_at = timezone.now()
        prod.save()
        messages.success(request, f'Mail wysłany do: {", ".join(recipients)}')
    else:
        messages.error(request, f'Błąd wysyłki: {error_msg}')

    return redirect('production_detail', pk=pk)


def _build_email_body(prod: FirstProduction) -> str:
    def name(user):
        return user.get_full_name() if user else '–'

    lines = [
        f'Pierwsza produkcja: {prod.product_name}',
        f'Zlecenie SAP:      {prod.sap_zlecenie or "–"}',
        f'Nr materiału SAP:  {prod.sap_material or "–"}',
        f'Data produkcji:    {prod.data_produkcji or "–"}',
        f'Typ produkcji:     {prod.typ_produkcji or "–"}',
        f'Layout:            {prod.layout or "–"}',
        f'Zmiany:            {prod.zmiany or "–"}',
        f'Linia pakująca:    {prod.packaging_line or "–"}',
        '',
        f'Osoba SD:          {name(prod.person_sd)}',
        f'Akceptujący:       {name(prod.acceptor)}',
        '',
        'Zespół:',
        f'  R&D: {name(prod.person_rd)}',
        f'  SC:  {name(prod.person_sc)}',
        f'  QA:  {name(prod.person_qa)}',
        f'  PP:  {name(prod.person_pp)}',
        f'  CE:  {name(prod.person_ce)}',
        '',
        '-- FirstTrack, H. & J. Brüggen KG --',
    ]
    return '\n'.join(lines)


def _notify_new_productions(productions):
    """Mail do stałej puli adresów o nowo dodanych produkcjach – trzeba
    uzupełnić dane oraz przypisać zespoły na zbliżające się produkcje."""
    productions = list(productions)
    if not productions:
        return

    recipients = sorted(set(
        NotificationRecipient.objects.filter(active=True).values_list('email', flat=True)
    ))
    if not recipients:
        return

    subject = (
        f'[FirstTrack] Nowa produkcja do uzupełnienia: {productions[0].product_name}'
        if len(productions) == 1
        else f'[FirstTrack] Nowe produkcje do uzupełnienia ({len(productions)})'
    )

    def col(value, width):
        text = str(value) if value else '–'
        return text[:width].ljust(width)

    header = f'{col("Zlecenie SAP", 14)}{col("Nr materiału", 14)}{col("Produkt", 40)}{col("Data produkcji", 16)}'
    rows = [
        f'{col(p.sap_zlecenie, 14)}{col(p.sap_material, 14)}{col(p.product_name, 40)}{col(p.data_produkcji, 16)}'
        for p in productions
    ]

    lines = [
        'W systemie FirstTrack dodano nowe pierwsze produkcje.',
        'Uzupełnij brakujące dane oraz przypisz zespoły na zbliżające się produkcje.',
        '',
        header,
        '-' * len(header),
        *rows,
        '',
        f'Otwórz aplikację: {settings.FIRSTTRACK_APP_URL}',
        '',
        '-- FirstTrack, H. & J. Brüggen KG --',
    ]
    body = '\n'.join(lines)

    success = True
    error_msg = ''
    try:
        send_mail(subject=subject, message=body,
                  from_email=settings.DEFAULT_FROM_EMAIL,
                  recipient_list=recipients, fail_silently=False)
    except Exception as e:
        success = False
        error_msg = str(e)
        logger.error('Błąd wysyłki maila o nowych produkcjach: %s', e)

    for p in productions:
        EmailLog.objects.create(
            production=p, recipient=', '.join(recipients),
            subject=subject, body=body, success=success, error_msg=error_msg,
        )


def _production_team_recipients(prod):
    """Stała pula adresów + osoby przypisane do zespołu tej konkretnej produkcji."""
    recipients = set(
        NotificationRecipient.objects.filter(active=True).values_list('email', flat=True)
    )
    team_fields = [
        'person_rd', 'person_sc', 'person_ql', 'person_qa',
        'person_sd', 'person_sdp', 'person_pp', 'person_ce',
    ]
    for field in team_fields:
        person = getattr(prod, field)
        if person and person.email:
            recipients.add(person.email)
    return sorted(recipients)


def _send_and_log(prod, subject, body, recipients, email_message=None):
    """Wysyła maila (plain-text albo gotowy EmailMessage z załącznikami) i zawsze
    zapisuje próbę w EmailLog, niezależnie od wyniku."""
    success = True
    error_msg = ''
    try:
        if email_message is not None:
            email_message.send(fail_silently=False)
        else:
            send_mail(subject=subject, message=body,
                      from_email=settings.DEFAULT_FROM_EMAIL,
                      recipient_list=recipients, fail_silently=False)
    except Exception as e:
        success = False
        error_msg = str(e)
        logger.error('Błąd wysyłki maila (%s): %s', subject, e)

    EmailLog.objects.create(
        production=prod, recipient=', '.join(recipients),
        subject=subject, body=body, success=success, error_msg=error_msg,
    )
    return success


def _send_due_production_reminders():
    """Przypomnienie o produkcji zaplanowanej na dziś – wysyłane raz dziennie,
    przy pierwszym wejściu na dashboard po północy (bez potrzeby harmonogramu)."""
    today = timezone.localdate()
    due = (
        FirstProduction.objects
        .exclude(status='zwolniona')
        .filter(data_produkcji=today)
        .exclude(reminder_sent_at__date=today)
    )
    for prod in due:
        recipients = _production_team_recipients(prod)
        if not recipients:
            continue
        subject = f'[FirstTrack] Przypomnienie: dziś produkcja – {prod.product_name}'
        body = '\n'.join([
            f'Dziś ({today:%Y-%m-%d}) zaplanowana jest pierwsza produkcja:',
            '',
            f'Produkt:           {prod.product_name}',
            f'Zlecenie SAP:      {prod.sap_zlecenie or "–"}',
            f'Nr materiału SAP:  {prod.sap_material or "–"}',
            f'Linia pakująca:    {prod.packaging_line or "–"}',
            f'Zmiany:            {prod.zmiany or "–"}',
            '',
            f'Otwórz aplikację: {settings.FIRSTTRACK_APP_URL}',
            '',
            '-- FirstTrack, H. & J. Brüggen KG --',
        ])
        if _send_and_log(prod, subject, body, recipients):
            prod.reminder_sent_at = timezone.now()
            prod.save(update_fields=['reminder_sent_at'])


def _send_sensory_accepted_email(prod, ca):
    recipients = _production_team_recipients(prod)
    if not recipients:
        return
    signed = [
        (role, person.get_full_name() or person.username)
        for role, fname, person in _all_sig_fields(prod)
        if person and getattr(ca, fname)
    ]
    signed_lines = [f'  {role}: {name}' for role, name in signed] or ['  (brak podpisów)']
    subject = f'[FirstTrack] Sensoryka zaakceptowana – {prod.product_name}'
    body = '\n'.join([
        f'Parametry sensoryczne dla produkcji „{prod.product_name}" zostały zaakceptowane.',
        'Receptura została zatwierdzona w gronie zespołu – zgoda na przejście do pakowania.',
        '',
        'Zespół, który zaakceptował:',
        *signed_lines,
        '',
        '-- FirstTrack, H. & J. Brüggen KG --',
    ])
    _send_and_log(prod, subject, body, recipients)


def _send_packaging_accepted_email(prod):
    recipients = _production_team_recipients(prod)
    if not recipients:
        return
    subject = f'[FirstTrack] Pakowanie zaakceptowane – {prod.product_name}'
    body = '\n'.join([
        f'Etap II (pakowanie) dla produkcji „{prod.product_name}" został zaakceptowany.',
        '',
        '-- FirstTrack, H. & J. Brüggen KG --',
    ])
    _send_and_log(prod, subject, body, recipients)


def _send_release_email(prod, ca):
    recipients = _production_team_recipients(prod)
    if not recipients:
        return

    subject = f'[FirstTrack] Zwolniona do sprzedaży – {prod.product_name}'
    body = '\n'.join([
        f'Zamówienie „{prod.product_name}" (zlecenie SAP {prod.sap_zlecenie or "–"}) '
        f'zostało zwolnione do sprzedaży.',
        f'Liczba UMK do śluzy: {ca.umk_count or "–"}',
        '',
        'W załączniku: checklista końcowa (PDF) oraz zdjęcia z produkcji.',
        '',
        '-- FirstTrack, H. & J. Brüggen KG --',
    ])

    email = EmailMessage(subject=subject, body=body,
                         from_email=settings.DEFAULT_FROM_EMAIL, to=recipients)
    try:
        from .pdf_views import _render_pdf, _build_team_sigs
        cb = getattr(prod, 'checklist_before', None)
        pdf_bytes = _render_pdf('productions/pdf/etap3.html', {
            'production': prod, 'cb': cb, 'ca': ca,
            'sensory': ca.sensory_params.all(),
            'packaging': ca.packaging_items.all(),
            'team_sigs': _build_team_sigs(prod, ca),
        })
        email.attach(f'PP_{prod.sap_zlecenie}_Zwolnienie.pdf', pdf_bytes, 'application/pdf')
        for field in ['photo_1', 'photo_2', 'photo_3', 'photo_4']:
            photo = getattr(ca, field)
            if photo:
                email.attach_file(photo.path)
    except Exception as e:
        logger.error('Błąd generowania załączników maila o zwolnieniu: %s', e)

    _send_and_log(prod, subject, body, recipients, email_message=email)


# ══════════════════════════════════════════════
# Zarządzanie użytkownikami
# ══════════════════════════════════════════════

@login_required
def user_list(request):
    users = User.objects.select_related('profile').order_by(
        'profile__department', 'last_name', 'first_name'
    )
    # Grupuj po dziale
    by_dept = {}
    for u in users:
        dept = getattr(u, 'profile', None)
        dept_label = dept.get_department_display() if dept and dept.department else 'Brak działu'
        by_dept.setdefault(dept_label, []).append(u)

    return render(request, 'productions/user_list.html', {
        'users_by_dept': by_dept,
        'total': users.count(),
    })


@login_required
def user_create(request):
    form = UserCreateForm()
    if request.method == 'POST':
        form = UserCreateForm(request.POST)
        if form.is_valid():
            d = form.cleaned_data
            # username = email (przed @)
            username_base = d['email'].split('@')[0].lower()
            username = username_base
            counter = 1
            while User.objects.filter(username=username).exists():
                username = f'{username_base}{counter}'
                counter += 1

            user = User.objects.create_user(
                username=username,
                email=d['email'],
                first_name=d['first_name'],
                last_name=d['last_name'],
            )
            # Numer chip zastępuje hasło – ustawiany też jako hasło Django,
            # żeby ten sam numer działał do logowania w panelu /admin/.
            user.set_password(d['chip_number'])
            user.save()
            profile, _ = UserProfile.objects.get_or_create(user=user)
            profile.department = d['department']
            profile.phone = d.get('phone', '')
            profile.chip_number = d['chip_number']
            profile.save()

            messages.success(request, f'Konto dla {user.get_full_name()} zostało utworzone.')
            return redirect('user_list')

    return render(request, 'productions/user_form.html', {
        'form': form, 'title': 'Nowe konto użytkownika', 'is_create': True,
    })


@login_required
def user_edit(request, pk):
    user = get_object_or_404(User, pk=pk)
    profile, _ = UserProfile.objects.get_or_create(user=user)

    initial = {
        'first_name': user.first_name,
        'last_name':  user.last_name,
        'email':      user.email,
        'department': profile.department,
        'phone':      profile.phone,
        'is_active':  user.is_active,
        'is_staff':   user.is_staff,
    }
    form = UserEditForm(initial=initial)

    if request.method == 'POST':
        form = UserEditForm(request.POST)
        if form.is_valid():
            d = form.cleaned_data
            user.first_name = d['first_name']
            user.last_name  = d['last_name']
            user.email      = d['email']
            user.is_active  = d['is_active']
            user.is_staff   = d['is_staff']
            user.save()
            profile.department = d['department']
            profile.phone      = d.get('phone', '')
            profile.save()
            messages.success(request, f'Dane użytkownika {user.get_full_name()} zaktualizowane.')
            return redirect('user_list')

    return render(request, 'productions/user_form.html', {
        'form': form, 'edit_user': user, 'title': f'Edytuj: {user.get_full_name()}',
    })


@login_required
def user_chip(request, pk):
    user = get_object_or_404(User, pk=pk)
    profile, _ = UserProfile.objects.get_or_create(user=user)
    form = UserChipForm(user=user)

    if request.method == 'POST':
        form = UserChipForm(request.POST, user=user)
        if form.is_valid():
            chip_number = form.cleaned_data['chip_number']
            profile.chip_number = chip_number
            profile.save()
            user.set_password(chip_number)
            user.save()
            messages.success(request, f'Numer chip dla {user.get_full_name()} został zmieniony.')
            return redirect('user_list')

    return render(request, 'productions/user_chip.html', {
        'form': form, 'edit_user': user,
    })


# ══════════════════════════════════════════════
# Zarządzanie stałą pulą adresów email (pierwsza produkcja)
# ══════════════════════════════════════════════

@login_required
def notification_email_list(request):
    form = NotificationRecipientForm()
    if request.method == 'POST':
        form = NotificationRecipientForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, 'Adres email został dodany do stałej puli.')
            return redirect('notification_email_list')

    acs_configured = bool(settings.ACS_EMAIL_CONNECTION_STRING and settings.ACS_EMAIL_SENDER_ADDRESS)

    return render(request, 'productions/notification_email_list.html', {
        'form': form,
        'recipients': NotificationRecipient.objects.all(),
        'acs_configured': acs_configured,
        'recent_logs': EmailLog.objects.select_related('production').all()[:15],
    })


@login_required
@require_POST
def notification_email_delete(request, pk):
    recipient = get_object_or_404(NotificationRecipient, pk=pk)
    recipient.delete()
    messages.success(request, f'Adres {recipient.email} został usunięty ze stałej puli.')
    return redirect('notification_email_list')


@login_required
@require_POST
def notification_email_test(request):
    """Wysyłka testowego maila – do zweryfikowania konfiguracji wysyłki
    (Azure Communication Services albo plik lokalny w trybie dev)."""
    test_email = request.POST.get('test_email', '').strip()
    if test_email:
        recipients = [test_email]
    else:
        recipients = sorted(set(
            NotificationRecipient.objects.filter(active=True).values_list('email', flat=True)
        ))

    if not recipients:
        messages.error(
            request,
            'Brak adresu do testu – wpisz adres testowy albo dodaj przynajmniej '
            'jeden aktywny adres do stałej puli.',
        )
        return redirect('notification_email_list')

    acs_configured = bool(settings.ACS_EMAIL_CONNECTION_STRING and settings.ACS_EMAIL_SENDER_ADDRESS)
    subject = '[FirstTrack] Testowy mail'
    body = '\n'.join([
        'To jest testowy mail z systemu FirstTrack.',
        '',
        f'Backend wysyłki: {"Azure Communication Services" if acs_configured else "plik lokalny (tryb dev, brak danych ACS)"}',
        '',
        'Jeśli ten mail dotarł na skrzynkę – wysyłka działa poprawnie.',
        '',
        '-- FirstTrack, H. & J. Brüggen KG --',
    ])

    success = True
    error_msg = ''
    try:
        send_mail(subject=subject, message=body, from_email=settings.DEFAULT_FROM_EMAIL,
                  recipient_list=recipients, fail_silently=False)
    except Exception as e:
        success = False
        error_msg = str(e)
        logger.error('Błąd wysyłki testowego maila: %s', e)

    EmailLog.objects.create(
        production=None, recipient=', '.join(recipients),
        subject=subject, body=body, success=success, error_msg=error_msg,
    )

    if success:
        messages.success(request, f'Testowy mail wysłany do: {", ".join(recipients)}')
    else:
        messages.error(request, f'Błąd wysyłki testowego maila: {error_msg}')
    return redirect('notification_email_list')

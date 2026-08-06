import json
import base64
import logging
from .pdf_views import pdf_etap1, pdf_etap2, pdf_etap3
from django.core.cache import cache
from django.shortcuts import render, get_object_or_404, redirect
from django.urls import reverse
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
    ChecklistAfterHeaderForm, LinkPackagingForm,
    SensoryParamFormSet, PackagingItemFormSet,
    UserCreateForm, UserEditForm, UserChipForm, UserImportRowForm, UserBulkImportForm,
    NotificationRecipientForm,
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

def _default_scope_for_material(sap_material):
    """Zlecenia z 5-cyfrowym numerem materiału są zwykle samym pakowaniem
    (FERT); pozostałe domyślnie zakładamy jako pełny proces (sensoryka +
    pakowanie) - użytkownik może to zmienić per wiersz przed dodaniem."""
    material = (sap_material or '').strip()
    return 'packaging' if material.isdigit() and len(material) == 5 else 'full'


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
    if prefill:
        for row in prefill:
            row['scope'] = _default_scope_for_material(row.get('sap_material'))

    return render(request, 'productions/import_sap.html', {
        'form': form,
        'extracted': prefill,
        'scope_choices': FirstProduction.SCOPE_CHOICES,
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
# API – prefill z SAP
# ──────────────────────────────────────────────

@login_required
@require_POST
def api_prefill_sap(request):
    try:
        rows = json.loads(request.body)
        created_productions = []
        skipped_zlecenia = []
        for data in rows:
            sap_zlecenie = (data.get('sap_zlecenie') or '').strip()
            # AI mogła odczytać zlecenie, które już jest w systemie - pomijamy je,
            # żeby nie dodawać tej samej produkcji drugi raz.
            if sap_zlecenie and FirstProduction.objects.filter(sap_zlecenie=sap_zlecenie).exists():
                skipped_zlecenia.append(sap_zlecenie)
                continue
            date_val = data.get('data_produkcji') or None
            scope = data.get('scope') or 'full'
            if scope not in dict(FirstProduction.SCOPE_CHOICES):
                scope = 'full'
            prod = FirstProduction.objects.create(
                sap_zlecenie=sap_zlecenie,
                sap_material=data.get('sap_material', ''),
                product_name=data.get('product_name', '') or 'Nowa produkcja',
                data_produkcji=date_val,
                scope=scope,
            )
            created_productions.append(prod)
        _notify_new_productions(created_productions)
        return JsonResponse({
            'ok': True,
            'created': len(created_productions),
            'skipped_existing': skipped_zlecenia,
        })
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

    form = FirstProductionForm(initial=initial, user=request.user)
    if request.method == 'POST':
        form = FirstProductionForm(request.POST, request.FILES, user=request.user)
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
    form = FirstProductionForm(instance=prod, user=request.user)
    if request.method == 'POST':
        form = FirstProductionForm(request.POST, request.FILES, instance=prod, user=request.user)
        if form.is_valid():
            prod = form.save()
            if prod.acceptor and not prod.acceptor_email:
                prod.acceptor_email = prod.acceptor.email
                prod.save(update_fields=['acceptor_email'])
            messages.success(request, 'Dane produkcji zaktualizowane.')
            return redirect('production_detail', pk=prod.pk)

    link_form = None
    if prod.is_sensory_only and not prod.linked_production:
        link_form = LinkPackagingForm(production=prod)
        # Formularz łączenia jest zadeklarowany w szablonie poza głównym
        # <form> tej strony (zagnieżdżone <form> są nieprawidłowym HTML-em i
        # psują submit obu formularzy) - to pole musi więc wskazywać na
        # niego przez atrybut form=, żeby jego wartość została wysłana.
        link_form.fields['packaging_production'].widget.attrs['form'] = 'link-packaging-form'

    return render(request, 'productions/production_form.html', {
        'form': form, 'production': prod,
        'title': f'Edytuj: {prod.product_name}',
        'link_form': link_form,
    })


# ──────────────────────────────────────────────
# Szczegóły produkcji
# ──────────────────────────────────────────────

@login_required
def production_detail(request, pk):
    prod = get_object_or_404(
        FirstProduction.objects.select_related(
            'person_rd', 'person_sc', 'person_ql', 'person_qa',
            'person_sd', 'person_pp', 'person_ce', 'person_te',
            'acceptor', 'linked_production',
        ),
        pk=pk
    )

    if prod.status == 'zwolniona':
        edit_form = None
    elif request.method == 'POST' and 'save_basic' in request.POST:
        edit_form = FirstProductionForm(request.POST, instance=prod, user=request.user)
        if edit_form.is_valid():
            edit_form.save()
            messages.success(request, 'Dane produkcji zaktualizowane.')
            return redirect('production_detail', pk=pk)
    else:
        edit_form = FirstProductionForm(instance=prod, user=request.user)

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

    form = ChecklistBeforeForm(instance=instance, initial={'packaging_line': prod.packaging_line})
    if request.method == 'POST':
        form = ChecklistBeforeForm(request.POST, instance=instance)
        if form.is_valid():
            cb = form.save(commit=False)
            cb.production = prod
            # Linia pakująca to pole FirstProduction, nie ChecklistBefore -
            # wpisywana tutaj (Etap I), więc zapisywana na produkcji.
            prod.packaging_line = form.cleaned_data.get('packaging_line', '')
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
            # Liczba UMK do śluzy pochodzi z Etapu I ("Wymagane dodatkowe
            # próbki dla klienta? Ilość:") - nie jest już wpisywana ręcznie
            # w Etapie III.
            umk_count=cb.additional_samples_count if cb else '',
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
            if not instance.umk_count and cb.additional_samples_count:
                instance.umk_count = cb.additional_samples_count
                update_fields.append('umk_count')
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
        ('PP',  'sig_pp',  prod.person_pp),
        ('CE',  'sig_ce',  prod.person_ce),
        ('Technologia', 'sig_te', prod.person_te),
    ]


# Etap II krok 1 – sensoryczne
@login_required
def checklist_after(request, pk):
    prod = get_object_or_404(FirstProduction, pk=pk)
    if prod.skips_sensory:
        # Produkcja "tylko pakowanie" nie ma własnego etapu sensorycznego -
        # sensoryka jest pomijana i nie bierze udziału w tym procesie.
        return redirect('checklist_after_packaging', pk=pk)
    return redirect('checklist_after_sensory', pk=pk)


@login_required
def checklist_after_sensory(request, pk):
    prod = get_object_or_404(FirstProduction, pk=pk)
    if prod.skips_sensory:
        return redirect('checklist_after_packaging', pk=pk)

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
            if 'next' in request.POST:
                _send_sensory_accepted_email(prod, ca)
                if prod.is_sensory_only:
                    # Brak własnego etapu pakowania - sensoryka kończy Etap II
                    # tej produkcji (pakowanie realizuje powiązane zlecenie).
                    ca.completed_at = timezone.now()
                    ca.save(update_fields=['completed_at'])
                    prod.status = 'etap2'
                    prod.save()
                    messages.success(request, 'Parametry sensoryczne zaakceptowane. Etap II zatwierdzony.')
                    return redirect('production_detail', pk=pk)
                messages.success(request, 'Parametry sensoryczne zapisane.')
                return redirect('checklist_after_packaging', pk=pk)
            messages.success(request, 'Parametry sensoryczne zapisane.')
            return redirect('checklist_after_sensory', pk=pk)

    link_form = None
    if prod.is_sensory_only and not prod.linked_production:
        link_form = LinkPackagingForm(production=prod)

    return render(request, 'productions/checklist_after_sensory.html', {
        'form': form,
        'sensory_fs': sensory_fs,
        'production': prod,
        'checklist': instance,
        'team_sig_fields': _all_sig_fields(prod),
        'link_form': link_form,
        'step': 1,
    })


def _link_redirect_target(request, prod, default_view_name):
    """Zarówno checklista sensoryczna, jak i formularz edycji produkcji mają
    ten sam formularz powiązania z pakowaniem - po zapisaniu wracamy tam,
    skąd przyszło żądanie, a nie zawsze na checklistę."""
    next_url = request.POST.get('next', '')
    if next_url and url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}):
        return redirect(next_url)
    return redirect(default_view_name, pk=prod.pk)


@login_required
@require_POST
def link_packaging_production(request, pk):
    """Powiąż produkcję "tylko sensoryka" ze zleceniem "tylko pakowanie" po
    numerze zlecenia SAP - checklisty obu stron stają się widoczne wzajemnie."""
    prod = get_object_or_404(FirstProduction, pk=pk)
    if not prod.is_sensory_only:
        messages.error(request, 'Powiązanie pakowni jest dostępne tylko dla produkcji „tylko sensoryka".')
        return _link_redirect_target(request, prod, 'checklist_after_sensory')

    form = LinkPackagingForm(request.POST, production=prod)
    if form.is_valid():
        packaging_prod = form.cleaned_data['packaging_production']
        now = timezone.now()
        prod.linked_production = packaging_prod
        prod.linked_at = now
        prod.linked_by = request.user
        prod.save(update_fields=['linked_production', 'linked_at', 'linked_by'])
        packaging_prod.linked_production = prod
        packaging_prod.linked_at = now
        packaging_prod.linked_by = request.user
        packaging_prod.save(update_fields=['linked_production', 'linked_at', 'linked_by'])
        messages.success(
            request,
            f'Powiązano z pakowaniem „{packaging_prod.product_name}" '
            f'(zlecenie SAP {packaging_prod.sap_zlecenie or "–"}).',
        )
    else:
        messages.error(request, 'Nie udało się powiązać - wybierz zlecenie pakowania z listy.')
    return _link_redirect_target(request, prod, 'checklist_after_sensory')


@login_required
@require_POST
def unlink_production(request, pk):
    prod = get_object_or_404(FirstProduction, pk=pk)
    other = prod.linked_production
    if other:
        FirstProduction.objects.filter(pk=other.pk).update(
            linked_production=None, linked_at=None, linked_by=None)
    prod.linked_production = None
    prod.linked_at = None
    prod.linked_by = None
    prod.save(update_fields=['linked_production', 'linked_at', 'linked_by'])
    messages.success(request, 'Powiązanie zostało usunięte.')
    default_view = 'checklist_after_sensory' if prod.is_sensory_only else 'checklist_after_packaging'
    return _link_redirect_target(request, prod, default_view)


# Etap II krok 2 – pakowanie
@login_required
def checklist_after_packaging(request, pk):
    prod = get_object_or_404(FirstProduction, pk=pk)
    if prod.is_sensory_only:
        return redirect('checklist_after_sensory', pk=pk)

    instance = _get_or_create_checklist_after(prod)
    packaging_fs = PackagingItemFormSet(queryset=instance.packaging_items.all(), prefix='packaging')
    form = ChecklistAfterPackagingForm(instance=instance)
    # Dostępne też ze strony powiązanej produkcji sensorycznej ("Pakowanie
    # (powiązane zlecenie)") - next pozwala po zatwierdzeniu wrócić tam,
    # skąd użytkownik faktycznie przyszedł, a nie zawsze na tę (pakującą)
    # produkcję.
    next_url = request.GET.get('next', '')

    all_sigs = _all_sig_fields(prod)
    unsigned = [(role, fname, person) for role, fname, person in all_sigs
                if person and not getattr(instance, fname)]

    if request.method == 'POST':
        next_url = request.POST.get('next', '')
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
            # Etap pakowania nie generuje już własnego maila - jego treść
            # trafia dodatkowo do końcowego maila ze zwolnieniem (Etap III).
            messages.success(request, 'Etap II zatwierdzony.' if 'complete' in request.POST else 'Checklista pakowania zapisana.')
            if 'complete' in request.POST:
                return _link_redirect_target(request, prod, 'production_detail')
            return redirect(f"{reverse('checklist_after_packaging', args=[pk])}{'?next=' + next_url if next_url else ''}")

    linked_sensory = None
    if prod.is_packaging_only and prod.linked_production:
        linked_ca = getattr(prod.linked_production, 'checklist_after', None)
        linked_sensory = {
            'production': prod.linked_production,
            'checklist': linked_ca,
            'sensory_params': linked_ca.sensory_params.all() if linked_ca else [],
        }

    return render(request, 'productions/checklist_after_packaging.html', {
        'form': form,
        'packaging_fs': packaging_fs,
        'production': prod,
        'checklist': instance,
        'unsigned_sig_fields': unsigned,
        'linked_sensory': linked_sensory,
        'step': 2,
        'next_url': next_url,
    })


# ──────────────────────────────────────────────
# Etap III – Decyzja SD (Akceptacja / Akceptacja warunkowa / Do korekty)
#            oraz zwolnienie do sprzedaży
# ──────────────────────────────────────────────

def _linked_target_for_stage(prod, stage):
    """Dla powiązanej pary sensoryka/pakowanie dane danego etapu (sensoryczne
    albo pakowania) fizycznie żyją na tej z dwóch produkcji, która faktycznie
    go wykonuje - dla produkcji "tylko sensoryka" to parametry sensoryczne są
    u niej, ale pozycje pakowania są u powiązanej produkcji "tylko
    pakowanie" (i odwrotnie). Zwraca właściwą produkcję do zresetowania."""
    if stage == 'packaging' and prod.is_sensory_only and prod.linked_production:
        return prod.linked_production
    if stage == 'sensory' and prod.is_packaging_only and prod.linked_production:
        return prod.linked_production
    return prod


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
            decision = ca.decision

            if decision == 'correction':
                ca.final_acceptance = False
                ca.completed_at = None
                ca.save()
                stage = ca.correction_return_stage
                reset_prod = _linked_target_for_stage(prod, stage)
                reset_ca = _get_or_create_checklist_after(reset_prod)
                if stage == 'sensory':
                    reset_ca.sensory_params.all().update(status='', uwagi='', korekta='', kto='', kiedy='')
                elif stage == 'packaging':
                    reset_ca.packaging_items.all().update(status='', uwagi='', korekta='', kto='', kiedy='')
                prod.status = 'etap2'
                prod.save()
                # Powiązana produkcja sensoryka/pakowanie to w praktyce jedna
                # produkcja - korekta jednej strony musi też zresetować
                # status/ukończenie checklisty drugiej, inaczej po ponownym
                # zatwierdzeniu tej drugiej strony ta pierwsza (z której
                # wysłano do korekty) zostałaby błędnie oznaczona jako
                # ukończona/zaakceptowana, mimo że wciąż czeka na poprawki.
                if reset_prod.pk != prod.pk:
                    reset_ca.completed_at = None
                    reset_ca.final_acceptance = False
                    reset_ca.save(update_fields=['completed_at', 'final_acceptance'])
                    reset_prod.status = 'etap2'
                    reset_prod.save(update_fields=['status'])
                messages.warning(
                    request,
                    'Produkcja skierowana do korekty - checklista wybranego etapu '
                    'została zresetowana, a zespół powiadomiony mailem.',
                )
                _send_correction_email(prod, ca)
            else:
                ca.final_acceptance = True
                ca.acceptance_date = ca.acceptance_date or timezone.localdate()
                ca.save()
                prod.status = 'zwolniona'
                prod.save()
                if prod.linked_production:
                    linked = prod.linked_production
                    linked_ca = _get_or_create_checklist_after(linked)
                    linked_ca.final_acceptance = True
                    linked_ca.acceptance_date = ca.acceptance_date
                    linked_ca.save(update_fields=['final_acceptance', 'acceptance_date'])
                    linked.status = 'zwolniona'
                    linked.save(update_fields=['status'])
                messages.success(request, f'Produkcja „{prod.product_name}" zwolniona do sprzedaży.')
                _send_release_email(prod, ca)
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
        'person_sd', 'person_pp', 'person_ce', 'person_te',
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
        'person_sd', 'person_pp', 'person_ce', 'person_te',
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
        f'Zlecenie SAP:      {prod.sap_zlecenie or "–"}',
        f'Nr materiału SAP:  {prod.sap_material or "–"}',
        'Receptura została zatwierdzona w gronie zespołu – zgoda na przejście do pakowania.',
        '',
        'Zespół, który zaakceptował:',
        *signed_lines,
        '',
        '-- FirstTrack, H. & J. Brüggen KG --',
    ])
    _send_and_log(prod, subject, body, recipients)


def _packaging_accepted_lines(prod, ca):
    """Treść dawnego, samodzielnego maila o zaakceptowaniu pakowania - teraz
    dołączana jako fragment końcowego maila ze zwolnieniem (Etap III)."""
    signed = [
        (role, person.get_full_name() or person.username)
        for role, fname, person in _all_sig_fields(prod)
        if person and getattr(ca, fname)
    ]
    signed_lines = [f'  {role}: {name}' for role, name in signed] or ['  (brak podpisów)']
    return [
        f'Etap II (pakowanie) dla produkcji „{prod.product_name}" został zaakceptowany.',
        'Zespół, który zaakceptował pakowanie:',
        *signed_lines,
    ]


def _send_release_email(prod, ca):
    recipients = _production_team_recipients(prod)
    if not recipients:
        return

    subject = f'[FirstTrack] Zwolniona do sprzedaży – {prod.product_name}'
    body_lines = [
        f'Zamówienie „{prod.product_name}" (zlecenie SAP {prod.sap_zlecenie or "–"}, '
        f'nr materiału SAP {prod.sap_material or "–"}) zostało zwolnione do sprzedaży.',
        '',
    ]
    if ca.decision == 'conditional':
        body_lines += [
            'Akceptacja warunkowa - powód:',
            f'  {ca.conditional_comment or "–"}',
            '',
        ]
    body_lines += [
        *_packaging_accepted_lines(prod, ca),
        '',
        f'Liczba UMK do śluzy: {ca.umk_count or "–"}',
        '',
        'W załączniku: checklista końcowa (PDF) oraz zdjęcia z akceptacji.',
        '',
        '-- FirstTrack, H. & J. Brüggen KG --',
    ]
    body = '\n'.join(body_lines)

    email = EmailMessage(subject=subject, body=body,
                         from_email=settings.DEFAULT_FROM_EMAIL, to=recipients)
    try:
        from .pdf_views import _generate_pdf_etap3
        pdf_bytes = _generate_pdf_etap3(prod, ca)
        if pdf_bytes:
            email.attach(f'PP_{prod.sap_zlecenie}_Zwolnienie.pdf', pdf_bytes, 'application/pdf')
    except Exception as e:
        logger.error('Błąd generowania PDF do maila o zwolnieniu (mail zostanie wysłany bez PDF): %s', e)
    try:
        for field in ['photo_1', 'photo_2', 'photo_3', 'photo_4']:
            photo = getattr(ca, field)
            if photo:
                email.attach_file(photo.path)
    except Exception as e:
        logger.error('Błąd dołączania zdjęć do maila o zwolnieniu: %s', e)

    _send_and_log(prod, subject, body, recipients, email_message=email)


def _send_correction_email(prod, ca):
    """Powiadamia zespół, że SD skierowało produkcję do korekty - wskazuje
    etap do powtórzenia i komentarz osoby akceptującej (checklista tego etapu
    została w tym samym kroku zresetowana)."""
    recipients = _production_team_recipients(prod)
    if not recipients:
        return
    stage_label = dict(ChecklistAfter.RETURN_STAGE_CHOICES).get(ca.correction_return_stage, '–')
    acceptor_name = prod.acceptor.get_full_name() if prod.acceptor else '–'
    subject = f'[FirstTrack] Do korekty ({stage_label}) – {prod.product_name}'
    body = '\n'.join([
        f'Produkcja „{prod.product_name}" (zlecenie SAP {prod.sap_zlecenie or "–"}, '
        f'nr materiału SAP {prod.sap_material or "–"}) została skierowana do korekty przez SD ({acceptor_name}).',
        f'Etap do powtórzenia: {stage_label}',
        f'Komentarz SD: {ca.correction_comment or "–"}',
        '',
        f'Checklista etapu „{stage_label}" została zresetowana - uzupełnij ją ponownie.',
        '',
        '-- FirstTrack, H. & J. Brüggen KG --',
    ])
    _send_and_log(prod, subject, body, recipients)


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


def _sync_chip_password(user, chip_number):
    """Numer chip zastępuje hasło – ustawiany też jako hasło Django, żeby ten
    sam numer działał do logowania w panelu /admin/. Bez numeru (osoba jeszcze
    nie ma przypisanego chipu) konto dostaje hasło nieużywalne, zamiast
    hashować pusty string jako prawdziwe hasło."""
    if chip_number:
        user.set_password(chip_number)
    else:
        user.set_unusable_password()


def _create_user_account(cleaned):
    """Tworzy konto + profil na podstawie cleaned_data z UserCreateForm
    (albo równoważnego słownika, np. przy imporcie masowym z Excela)."""
    username_base = cleaned['email'].split('@')[0].lower()
    username = username_base
    counter = 1
    while User.objects.filter(username=username).exists():
        username = f'{username_base}{counter}'
        counter += 1

    user = User.objects.create_user(
        username=username,
        email=cleaned['email'],
        first_name=cleaned['first_name'],
        last_name=cleaned['last_name'],
    )
    _sync_chip_password(user, cleaned['chip_number'])
    user.save()
    # `user.profile` (nie osobne `UserProfile.objects.get_or_create(...)`) -
    # sygnał post_save utworzył już profil i przy tym ustawił na `user`
    # cache relacji odwrotnej. Zapytanie przez osobny manager zwróciłoby ten
    # sam wiersz z bazy, ale jako INNY obiekt Python - `user.profile` zostałby
    # przy starym (puste dane) obiekcie w cache, więc np. w wynikach importu
    # masowego (ten sam `user` w pamięci przez cały request) numer chipu i
    # dział wyglądałyby na niewypełnione, mimo że w bazie są zapisane poprawnie.
    profile = user.profile
    profile.department = cleaned['department']
    profile.chip_number = cleaned['chip_number']
    profile.save()
    return user


def _update_user_account(user, cleaned):
    """Nadpisuje dane istniejącego konta (dopasowanego po emailu) wartościami
    z wiersza importu, żeby powtórny import tego samego arkusza aktualizował
    istniejące osoby, a nie wywalał się na 'ten adres email już istnieje'."""
    user.first_name = cleaned['first_name']
    user.last_name = cleaned['last_name']
    _sync_chip_password(user, cleaned['chip_number'])
    user.save()
    # get_or_create, nie `user.profile` - część starszych kont (z czasu przed
    # dodaniem modelu UserProfile) nie ma jeszcze wiersza profilu, więc samo
    # `user.profile` wywalałoby RelatedObjectDoesNotExist. `user` jest tu
    # świeżo pobrany w tym samym wywołaniu, więc problem stałego cache z
    # `_create_user_account` (patrz komentarz tam) nie ma zastosowania.
    profile, _ = UserProfile.objects.get_or_create(user=user)
    profile.department = cleaned['department']
    profile.chip_number = cleaned['chip_number']
    profile.save()
    return user


@login_required
def user_create(request):
    form = UserCreateForm()
    if request.method == 'POST':
        form = UserCreateForm(request.POST)
        if form.is_valid():
            user = _create_user_account(form.cleaned_data)
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
        'full_name':  f'{user.first_name} {user.last_name}'.strip(),
        'email':      user.email,
        'department': profile.department,
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
            profile.save()
            messages.success(request, f'Dane użytkownika {user.get_full_name()} zaktualizowane.')
            return redirect('user_list')

    return render(request, 'productions/user_form.html', {
        'form': form, 'edit_user': user, 'title': f'Edytuj: {user.get_full_name()}',
    })


@login_required
@require_POST
def user_delete(request, pk):
    if not request.user.is_staff:
        messages.error(request, 'Brak uprawnień do usunięcia konta.')
        return redirect('user_list')
    user = get_object_or_404(User, pk=pk)
    if user.pk == request.user.pk:
        messages.error(request, 'Nie można usunąć własnego konta.')
        return redirect('user_list')
    name = user.get_full_name() or user.username
    user.delete()
    messages.success(request, f'Konto „{name}" zostało usunięte.')
    return redirect('user_list')


@login_required
def user_bulk_import(request):
    form = UserBulkImportForm()
    results = None

    if request.method == 'POST':
        form = UserBulkImportForm(request.POST, request.FILES)
        if form.is_valid():
            try:
                results = _import_users_from_excel(request.FILES['excel_file'])
            except ExcelImportError as e:
                messages.error(request, str(e))
                results = None
            else:
                if results['created']:
                    messages.success(request, f"Utworzono {len(results['created'])} kont.")
                if results['updated']:
                    messages.success(request, f"Zaktualizowano {len(results['updated'])} istniejących kont.")
                if results['errors']:
                    messages.warning(request, f"Pominięto {len(results['errors'])} wierszy - patrz szczegóły poniżej.")
                if not results['created'] and not results['updated'] and not results['errors']:
                    messages.error(request, 'Plik nie zawierał żadnych wierszy z danymi.')

    return render(request, 'productions/user_bulk_import.html', {
        'form': form, 'results': results,
    })


def _dept_lookup():
    """Mapuje kod działu albo jego etykietę (bez rozróżniania wielkości liter)
    na kod działu, np. {'rd': 'RD', 'r&d': 'RD', 'sd': 'SD', ...}."""
    lookup = {}
    for code, label in DEPT_CHOICES:
        lookup[code.strip().lower()] = code
        lookup[label.strip().lower()] = code
    return lookup


def _normalize_chip_cell(value):
    """Excel często przechowuje numer chip jako liczbę (np. 4821.0) - trzeba
    go z powrotem dopełnić zerami do 5 cyfr, żeby np. "04821" nie stało się "4821"."""
    if value is None:
        return ''
    if isinstance(value, float):
        return f'{int(value):05d}'
    if isinstance(value, int):
        return f'{value:05d}'
    return str(value).strip()


class ExcelImportError(Exception):
    """Podnoszony, gdy plik Excel nie mógł zostać przetworzony - np. pakiet
    openpyxl nie jest zainstalowany na serwerze, albo plik nie jest
    poprawnym .xlsx. Importujemy openpyxl leniwie (nie na poziomie modułu),
    żeby brak tej biblioteki nie wywalał całej aplikacji przy starcie, tylko
    konkretną akcję importu."""


def _import_users_from_excel(excel_file):
    """Parsuje plik .xlsx (kolumny: Imię i nazwisko, Email, Dział, Numer chip)
    i tworzy konta wiersz po wierszu - błąd w jednym wierszu nie przerywa
    importu pozostałych. Kolejność kolumn wg nagłówka (niezależna od wielkości
    liter), z fallbackiem na pozycję A/B/C/D, jeśli nagłówek nie jest znany."""
    try:
        import openpyxl
    except ImportError as e:
        logger.error('openpyxl nie jest zainstalowany - nie można zaimportować użytkowników: %s', e)
        raise ExcelImportError(
            'Nie udało się przetworzyć pliku: biblioteka openpyxl nie jest zainstalowana na serwerze. '
            'Skontaktuj się z administratorem systemu.'
        ) from e

    created, updated, errors = [], [], []
    try:
        wb = openpyxl.load_workbook(excel_file, data_only=True)
    except Exception as e:
        logger.error('Błąd wczytywania pliku Excel: %s', e, exc_info=True)
        raise ExcelImportError(
            'Nie udało się wczytać pliku - sprawdź, czy to poprawny plik .xlsx.'
        ) from e
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return {'created': created, 'updated': updated, 'errors': errors}

    header = [str(c or '').strip().lower() for c in rows[0]]
    col_map = {'imię i nazwisko': 0, 'imie i nazwisko': 0, 'email': 1, 'dział': 2, 'dzial': 2, 'numer chip': 3}
    idx = {'name': 0, 'email': 1, 'dept': 2, 'chip': 3}
    if any(h in col_map for h in header):
        for h_i, h in enumerate(header):
            if h in ('imię i nazwisko', 'imie i nazwisko'):
                idx['name'] = h_i
            elif h == 'email':
                idx['email'] = h_i
            elif h in ('dział', 'dzial'):
                idx['dept'] = h_i
            elif h == 'numer chip':
                idx['chip'] = h_i
        data_rows = rows[1:]
    else:
        data_rows = rows

    dept_lookup = _dept_lookup()
    for row_num, row in enumerate(data_rows, start=2):
        if not any(row):
            continue
        try:
            full_name = str(row[idx['name']] or '').strip()
            email = str(row[idx['email']] or '').strip()
            dept_raw = str(row[idx['dept']] or '').strip()
            chip = _normalize_chip_cell(row[idx['chip']])

            dept_code = dept_lookup.get(dept_raw.lower())
            if not dept_code:
                errors.append({'row': row_num, 'reason': f'Nieznany dział: „{dept_raw}".'})
                continue

            # Dopasowanie po emailu - jeśli konto już istnieje, wiersz
            # aktualizuje je, a nie wywala się na "email już istnieje" (co
            # pozwala uruchamiać ten sam arkusz wielokrotnie, np. po
            # poprawkach albo dopisaniu nowych osób).
            existing_user = User.objects.filter(email=email).first()
            form = UserImportRowForm(data={
                'full_name': full_name, 'email': email,
                'department': dept_code, 'chip_number': chip,
            }, existing_user=existing_user)
            if form.is_valid():
                if existing_user:
                    updated.append(_update_user_account(existing_user, form.cleaned_data))
                else:
                    created.append(_create_user_account(form.cleaned_data))
            else:
                reason = '; '.join(f'{f}: {"; ".join(e)}' for f, e in form.errors.items())
                errors.append({'row': row_num, 'reason': reason})
        except IndexError:
            errors.append({'row': row_num, 'reason': 'Wiersz ma mniej kolumn niż wymagane.'})
        except Exception as e:
            # Jeden zły wiersz (np. nieoczekiwany format komórki, konflikt w
            # bazie) nie może przerwać importu pozostałych wierszy.
            logger.error('Błąd importu wiersza %s z Excela: %s', row_num, e, exc_info=True)
            errors.append({'row': row_num, 'reason': f'Nieoczekiwany błąd: {e}'})

    return {'created': created, 'updated': updated, 'errors': errors}


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

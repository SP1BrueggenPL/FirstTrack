from django import forms
from django.contrib.auth.models import User
from .models import (
    FirstProduction, ChecklistBefore, ChecklistAfter,
    SensoryParam, PackagingItem, UserProfile, NotificationRecipient, DEPT_CHOICES,
)

ITEM_STATUS_CHOICES = [('', '–'), ('tak', 'Tak'), ('nie', 'Nie'), ('nd', 'N/D')]


def _fc(ph='', css='form-control form-control-sm'):
    return forms.TextInput(attrs={'class': css, 'placeholder': ph})

def _sel(css='form-select form-select-sm'):
    return forms.Select(attrs={'class': css})

def _date(css='form-control form-control-sm'):
    return forms.DateInput(attrs={'type': 'date', 'class': css})


# ──────────────────────────────────────────────────────────
# Użytkownicy
# ──────────────────────────────────────────────────────────

_CHIP_WIDGET = forms.TextInput(attrs={
    'class': 'form-control', 'inputmode': 'numeric', 'pattern': r'\d{5}',
    'maxlength': 5, 'placeholder': 'np. 04821', 'autocomplete': 'off',
})


def split_full_name(full_name):
    """Dzieli 'Imię i nazwisko' na (first_name, last_name) - ostatnie słowo
    to nazwisko, wszystko przed nim to imię (obsługuje imiona złożone)."""
    parts = (full_name or '').strip().split()
    if not parts:
        return '', ''
    if len(parts) == 1:
        return parts[0], ''
    return ' '.join(parts[:-1]), parts[-1]


def _clean_chip_number(chip, *, exclude_user=None):
    chip = (chip or '').strip()
    if not chip.isdigit() or len(chip) != 5:
        raise forms.ValidationError('Numer chip musi składać się z dokładnie 5 cyfr.')
    qs = UserProfile.objects.filter(chip_number=chip)
    if exclude_user is not None:
        qs = qs.exclude(user=exclude_user)
    if qs.exists():
        raise forms.ValidationError('Ten numer chip jest już przypisany do innego użytkownika.')
    return chip


class UserCreateForm(forms.Form):
    full_name = forms.CharField(label='Imię i nazwisko', max_length=150,
                                widget=_fc('np. Jan Kowalski', 'form-control'))
    email      = forms.EmailField(label='Email służbowy', required=False,
                                  widget=forms.EmailInput(attrs={'class': 'form-control'}))
    department = forms.ChoiceField(label='Dział', choices=[('', '– wybierz –')] + list(DEPT_CHOICES),
                                   widget=_sel('form-select'))
    chip_number = forms.CharField(label='Numer chip (5 cyfr)', widget=_CHIP_WIDGET)

    def clean_full_name(self):
        return (self.cleaned_data.get('full_name') or '').strip()

    def clean_email(self):
        email = self.cleaned_data.get('email', '')
        if email and User.objects.filter(email=email).exists():
            raise forms.ValidationError('Użytkownik z tym adresem email już istnieje.')
        return email

    def clean_chip_number(self):
        return _clean_chip_number(self.cleaned_data.get('chip_number'))

    def clean(self):
        cleaned = super().clean()
        if cleaned.get('full_name'):
            cleaned['first_name'], cleaned['last_name'] = split_full_name(cleaned['full_name'])
        return cleaned


class UserImportRowForm(UserCreateForm):
    """Wariant UserCreateForm dla importu masowego z Excela: powtórny import
    tego samego arkusza aktualizuje istniejące konto (dopasowane po emailu)
    zamiast wywalać się na 'ten adres email już istnieje', a puste pole
    numeru chip jest dopuszczalne - osoba jeszcze nie ma przypisanego chipu
    (w interfejsie wyświetli się jako „–"). Email jest tu (w odróżnieniu od
    ręcznego tworzenia konta) wymagany - import dopasowuje wiersz do
    istniejącego konta właśnie po adresie email."""
    email = forms.EmailField(label='Email służbowy',
                             widget=forms.EmailInput(attrs={'class': 'form-control'}))
    chip_number = forms.CharField(label='Numer chip (5 cyfr)', required=False, widget=_CHIP_WIDGET)

    def __init__(self, *args, existing_user=None, **kwargs):
        self._existing_user = existing_user
        super().__init__(*args, **kwargs)

    def clean_email(self):
        return self.cleaned_data['email']

    def clean_chip_number(self):
        chip = (self.cleaned_data.get('chip_number') or '').strip()
        if not chip:
            return ''
        return _clean_chip_number(chip, exclude_user=self._existing_user)


class UserEditForm(forms.Form):
    full_name = forms.CharField(label='Imię i nazwisko', max_length=150,
                                widget=_fc('np. Jan Kowalski', 'form-control'))
    email      = forms.EmailField(label='Email służbowy',
                                  widget=forms.EmailInput(attrs={'class': 'form-control'}))
    department = forms.ChoiceField(label='Dział', choices=[('', '– wybierz –')] + list(DEPT_CHOICES),
                                   widget=_sel('form-select'))
    is_active  = forms.BooleanField(label='Konto aktywne', required=False)
    is_staff   = forms.BooleanField(label='Admin', required=False)

    def clean_full_name(self):
        return (self.cleaned_data.get('full_name') or '').strip()

    def clean(self):
        cleaned = super().clean()
        if cleaned.get('full_name'):
            cleaned['first_name'], cleaned['last_name'] = split_full_name(cleaned['full_name'])
        return cleaned


class UserChipForm(forms.Form):
    chip_number = forms.CharField(label='Nowy numer chip (5 cyfr)', widget=_CHIP_WIDGET)

    def __init__(self, *args, user=None, **kwargs):
        self.user = user
        super().__init__(*args, **kwargs)

    def clean_chip_number(self):
        return _clean_chip_number(self.cleaned_data.get('chip_number'), exclude_user=self.user)


class UserBulkImportForm(forms.Form):
    excel_file = forms.FileField(
        label='Plik Excel (.xlsx)',
        help_text='Kolumny: Imię i nazwisko, Email, Dział, Numer chip.',
        widget=forms.FileInput(attrs={'class': 'form-control', 'accept': '.xlsx'}),
    )


# ──────────────────────────────────────────────────────────
# Stała pula adresów email (pierwsza produkcja)
# ──────────────────────────────────────────────────────────

class NotificationRecipientForm(forms.ModelForm):
    class Meta:
        model = NotificationRecipient
        fields = ['email', 'label', 'active']
        widgets = {
            'email': forms.EmailInput(attrs={'class': 'form-control', 'placeholder': 'adres@brueggen.com'}),
            'label': _fc('np. Dział jakości', 'form-control'),
            'active': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
        }


# ──────────────────────────────────────────────────────────
# Pierwsza Produkcja
# ──────────────────────────────────────────────────────────

def _use_full_name_labels(form, *field_names):
    """ModelChoiceField domyślnie pokazuje str(user), czyli login
    (imie.nazwisko) - nieczytelne w listach wyboru zespołu/akceptującego,
    więc wszędzie pokazujemy Imię i Nazwisko."""
    for name in field_names:
        form.fields[name].label_from_instance = lambda obj: obj.get_full_name() or obj.username


def _person_field(dept_code, label, empty_label='– wybierz –'):
    f = forms.ModelChoiceField(
        queryset=User.objects.filter(profile__department=dept_code)
                             .select_related('profile')
                             .order_by('last_name', 'first_name'),
        required=False,
        label=label,
        empty_label=empty_label,
        widget=forms.Select(attrs={'class': 'form-select form-select-sm person-select'}),
    )
    return f


# "Szczegółowe informacje"/"Numery" - pola edytowalne tylko przez dział
# nadzorujący (poza "komentarz", edytowalny przez każdego). Ten sam wzorzec
# blokowania (disabled=True) co CHECKLIST_BEFORE_ROW_FIELDS w views.py.
PRODUCTION_FIELD_DEPT_LOCKS = [
    (['scope'], ['SC']),
    (['data_produkcji', 'zmiany', 'layout', 'typ_produkcji', 'fert_number'], ['SD']),
    (['rd_number', 'recipe', 'crm_project_nr'], ['RD']),
]


class FirstProductionForm(forms.ModelForm):
    data_produkcji = forms.DateField(
        required=False,
        input_formats=['%Y-%m-%d'],
        widget=forms.DateInput(format='%Y-%m-%d', attrs={'type': 'date', 'class': 'form-control'}),
        label='Data produkcji',
    )

    class Meta:
        model = FirstProduction
        # Zespół (person_rd..person_sl) nie jest już wybierany tutaj - patrz
        # SENSORY_TEAM_FIELD_DEPTS/PACKAGING_TEAM_FIELD_DEPTS - wybierany jest
        # bezpośrednio na checkliście sensoryki/pakowania (Etap III), przez
        # dział, którego dotyczy dana rola.
        fields = [
            'sap_zlecenie', 'sap_material', 'product_name',
            'scope', 'data_produkcji', 'zmiany', 'layout', 'typ_produkcji', 'komentarz',
            'fert_number', 'rd_number', 'recipe', 'crm_project_nr',
            'acceptor',
        ]
        widgets = {
            'sap_zlecenie':   _fc('np. 11333525'),
            'sap_material':   _fc('np. 28124'),
            'product_name':   forms.TextInput(attrs={'class': 'form-control'}),
            'scope':          forms.Select(attrs={'class': 'form-select', 'id': 'id_scope'}),
            'data_produkcji': _date('form-control'),
            'zmiany':         _fc('np. nowy indeks, new article/new line'),
            'layout':         _fc('np. BMC, ND'),
            'typ_produkcji':  _sel('form-select form-select-sm'),
            'komentarz':      forms.Textarea(attrs={'class': 'form-control form-control-sm', 'rows': 2}),
            'fert_number':    _fc(css='form-control form-control-sm', ph='Numer FERT'),
            'rd_number':      _fc(),
            'recipe':         _fc(),
            'crm_project_nr': _fc(),
            'acceptor':       forms.Select(attrs={'class': 'form-select', 'id': 'id_acceptor'}),
        }

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        self.fields['acceptor'].queryset = (
            User.objects.filter(profile__department__in=['SD', 'CE'])
                        .select_related('profile')
                        .order_by('last_name', 'first_name')
        )
        self.fields['acceptor'].empty_label = '– wybierz akceptującego –'
        self.fields['acceptor'].required = False
        _use_full_name_labels(self, 'acceptor')
        # Żadne z pól "Szczegółowe informacje"/"Numery" (poza krótkim tekstem
        # materiału) nie jest wymagane - niezależnie od działu osoby
        # wypełniającej formularz, można zapisać produkcję z niekompletnymi
        # danymi i uzupełnić je później.
        for field_name in ('fert_number', 'recipe'):
            self.fields[field_name].required = False

        # disabled=True (nie tylko ukrycie w szablonie) - Django ignoruje
        # przesłaną wartość i przy zapisie zachowuje dotychczasową (patrz
        # BaseForm._clean_fields), więc ograniczenie działa nawet gdyby ktoś
        # ręcznie odblokował pole w przeglądarce. Administratorzy (is_staff)
        # i wywołania bez podanego usera (np. import) nie są ograniczane.
        if self.user is not None and not self.user.is_staff:
            dept = getattr(getattr(self.user, 'profile', None), 'department', '') or ''
            for field_names, allowed_depts in PRODUCTION_FIELD_DEPT_LOCKS:
                if dept in allowed_depts:
                    continue
                for field_name in field_names:
                    self.fields[field_name].disabled = True
            # Osoba zwalniająca (akceptor) jest wybierana tylko przez SD/CE -
            # to oni tworzą tę listę wyboru (queryset już ograniczony do
            # SD/CE), więc pole jest zablokowane dla każdego innego działu.
            if dept not in ('SD', 'CE'):
                self.fields['acceptor'].disabled = True

    def _user_label(self, user):
        return user.get_full_name() or user.username


class SAPImportForm(forms.Form):
    screenshot = forms.ImageField(
        label='Screenshot z SAP lub tabeli planowania',
        help_text='Obsługiwane: JPG, PNG, BMP, WEBP',
        widget=forms.FileInput(attrs={'class': 'form-control', 'accept': 'image/*'}),
    )


# ──────────────────────────────────────────────────────────
# Powiązanie: sensoryka (produkcja) ↔ pakowanie
# ──────────────────────────────────────────────────────────

class LinkPackagingForm(forms.Form):
    packaging_production = forms.ModelChoiceField(
        queryset=FirstProduction.objects.none(),
        label='Zlecenie pakowania do powiązania',
        empty_label='– wyszukaj zlecenie tylko pakowania –',
        widget=forms.Select(attrs={'class': 'form-select', 'id': 'id_packaging_production'}),
    )

    def __init__(self, *args, production=None, **kwargs):
        super().__init__(*args, **kwargs)
        qs = FirstProduction.objects.filter(scope='packaging', linked_production__isnull=True)
        if production is not None:
            qs = qs.exclude(pk=production.pk)
        self.fields['packaging_production'].queryset = qs.order_by('-created_at')


# ──────────────────────────────────────────────────────────
# Checklista Przed
# ──────────────────────────────────────────────────────────

class ChecklistBeforeForm(forms.ModelForm):
    # Linia pakująca to pole FirstProduction.packaging_line, nie ChecklistBefore -
    # wpisywana tutaj (Etap I), a nie w checkliście Etapu II (patrz widoki
    # checklist_before/_get_or_create_checklist_after).
    packaging_line = forms.CharField(
        label='Linia pakująca', max_length=50, required=False,
        widget=forms.TextInput(attrs={'class': 'form-control form-control-sm'}),
    )

    class Meta:
        model = ChecklistBefore
        fields = '__all__'
        # confirm_* nie są edytowalne przez formularz - zapisywane serwerowo
        # (patrz _stamp_checklist_before_confirmation w views.py) na podstawie
        # zalogowanego użytkownika, żeby nie dało się wpisać cudzego imienia.
        exclude = [
            'production', 'completed_at', 'created_at', 'updated_at',
            'confirm_rd', 'confirm_sd', 'confirm_sc', 'confirm_qa', 'confirm_ql', 'confirm_te', 'confirm_pp',
        ]
        widgets = {
            'order_updated_status':    forms.RadioSelect(attrs={'class': 'status-radio'}),
            'pwpr_status':             forms.RadioSelect(attrs={'class': 'status-radio'}),
            'analysis_form_status':    forms.RadioSelect(attrs={'class': 'status-radio'}),
            'zero_sample_status':      forms.RadioSelect(attrs={'class': 'status-radio'}),
            'production_card_status':  forms.RadioSelect(attrs={'class': 'status-radio'}),
            'machine_suitable_status': forms.RadioSelect(attrs={'class': 'status-radio'}),
            'packaging_layout_status': forms.RadioSelect(attrs={'class': 'status-radio'}),
            'collective_label_status': forms.RadioSelect(attrs={'class': 'status-radio'}),
            'date_format_status':      forms.RadioSelect(attrs={'class': 'status-radio'}),
            'bom_set_status':          forms.RadioSelect(attrs={'class': 'status-radio'}),
            'additional_samples_status': forms.RadioSelect(attrs={'class': 'status-radio'}),
            'test_packaging_1_status': forms.RadioSelect(attrs={'class': 'status-radio'}),
            'test_packaging_2_status': forms.RadioSelect(attrs={'class': 'status-radio'}),
            'test_packaging_3_status': forms.RadioSelect(attrs={'class': 'status-radio'}),
            'order_updated_uwagi':     forms.TextInput(attrs={'class': 'form-control form-control-sm'}),
            'pwpr_uwagi':              forms.TextInput(attrs={'class': 'form-control form-control-sm'}),
            'analysis_form_version':   forms.TextInput(attrs={'class': 'form-control form-control-sm', 'placeholder': 'Wersja dok.'}),
            'zero_sample_uwagi':       forms.TextInput(attrs={'class': 'form-control form-control-sm'}),
            'production_card_uwagi':   forms.TextInput(attrs={'class': 'form-control form-control-sm'}),
            'machine_suitable_uwagi':  forms.TextInput(attrs={'class': 'form-control form-control-sm'}),
            'packaging_layout_uwagi':  forms.TextInput(attrs={'class': 'form-control form-control-sm'}),
            'collective_label_uwagi':  forms.TextInput(attrs={'class': 'form-control form-control-sm'}),
            'date_format_uwagi':       forms.TextInput(attrs={'class': 'form-control form-control-sm'}),
            'bom_set_uwagi':           forms.TextInput(attrs={'class': 'form-control form-control-sm'}),
            'planned_yield_kg':        forms.TextInput(attrs={'class': 'form-control form-control-sm', 'placeholder': 'kg/h'}),
            'planned_yield_takty':     forms.TextInput(attrs={'class': 'form-control form-control-sm', 'placeholder': 'takty'}),
            'additional_samples_count': forms.TextInput(attrs={'class': 'form-control form-control-sm', 'placeholder': 'Ilość'}),
            'additional_samples_uwagi': forms.TextInput(attrs={'class': 'form-control form-control-sm', 'placeholder': 'Uwagi'}),
            'test_packaging_1_name':   forms.TextInput(attrs={'class': 'form-control form-control-sm', 'placeholder': 'Nazwa pozycji'}),
            'test_packaging_1_nadzor': forms.TextInput(attrs={'class': 'form-control form-control-sm', 'placeholder': 'Nadzór'}),
            'test_packaging_2_name':   forms.TextInput(attrs={'class': 'form-control form-control-sm', 'placeholder': 'Nazwa pozycji'}),
            'test_packaging_2_nadzor': forms.TextInput(attrs={'class': 'form-control form-control-sm', 'placeholder': 'Nadzór'}),
            'test_packaging_3_name':   forms.TextInput(attrs={'class': 'form-control form-control-sm', 'placeholder': 'Nazwa pozycji'}),
            'test_packaging_3_nadzor': forms.TextInput(attrs={'class': 'form-control form-control-sm', 'placeholder': 'Nadzór'}),
        }


# ──────────────────────────────────────────────────────────
# Checklista Po
# ──────────────────────────────────────────────────────────

_SIG_WIDGETS = {
    'sig_rd':  forms.HiddenInput(),
    'sig_sc':  forms.HiddenInput(),
    'sig_ql':  forms.HiddenInput(),
    'sig_qa':  forms.HiddenInput(),
    'sig_sd':  forms.HiddenInput(),
    'sig_pp':  forms.HiddenInput(),
    'sig_ce':  forms.HiddenInput(),
    'sig_te':  forms.HiddenInput(),
}
_SIG_FIELD_NAMES = list(_SIG_WIDGETS.keys())

# Sprzedaż Lubeck dokumentuje obecność zdjęciem, nie odręcznym podpisem -
# jedyna rola w zespole z osobnym polem pliku zamiast pola podpisu canvas.
_SL_PHOTO_WIDGET = forms.FileInput(attrs={
    'class': 'form-control form-control-sm', 'accept': 'image/*', 'capture': 'environment',
})

# Zespół nie jest już wybierany przy edycji produkcji - każdy etap ma inny
# zestaw ról do wyboru, uzupełniany bezpośrednio na jego checkliście przez
# osobę z odpowiedniego działu (patrz _stamp_...odpowiednie pola w views.py
# oraz _lock_team_fields_to_department poniżej). Pola person_* na tych
# formularzach NIE są polami modelu ChecklistAfter (żyją na FirstProduction)
# - są tu tylko do wyboru, a widok ręcznie przenosi je na produkcję (patrz
# checklist_after_sensory/checklist_after_packaging w views.py), tak jak już
# działa 'packaging_line' na ChecklistBeforeForm.
SENSORY_TEAM_FIELD_DEPTS = {
    'person_sd': 'SD', 'person_qa': 'QA', 'person_rd': 'RD',
    'person_te': 'TE', 'person_ce': 'CE',
}
PACKAGING_TEAM_FIELD_DEPTS = {
    'person_sd': 'SD', 'person_pp': 'PP', 'person_qa': 'QA',
    'person_ql': 'QL', 'person_ce': 'CE',
}


def _lock_team_fields_to_department(form, user, field_depts):
    """Każda rola w zespole jest wybierana tylko przez osobę z tego samego
    działu (np. RD nie może wybrać osoby SD) - poza Sprzedażą Lubeck, którą
    wybiera dział SD (SL pracuje zdalnie, nie loguje się do checklisty).
    Administratorzy (is_staff) mogą edytować wszystko."""
    if user is None or user.is_staff:
        return
    dept = getattr(getattr(user, 'profile', None), 'department', '') or ''
    for field_name, allowed_dept in field_depts.items():
        if dept != allowed_dept:
            form.fields[field_name].disabled = True
    if 'person_sl' in form.fields and dept != 'SD':
        form.fields['person_sl'].disabled = True


# Krok 1 – parametry sensoryczne
class ChecklistAfterSensoryForm(forms.ModelForm):
    production_date = forms.DateField(
        required=False,
        input_formats=['%Y-%m-%d'],
        widget=forms.DateInput(format='%Y-%m-%d', attrs={'type': 'date', 'class': 'form-control form-control-sm'}),
        label='Data produkcji',
    )
    person_sd = _person_field('SD', 'SD')
    person_qa = _person_field('QA', 'QA')
    person_rd = _person_field('RD', 'R&D')
    person_te = _person_field('TE', 'PT')
    person_ce = _person_field('CE', 'CE')
    person_sl = _person_field('SL', 'Sprzedaż Lubeck')
    # Pole zadeklarowane jawnie (nie przez ModelForm) - CharField z choices i
    # blank=True dostałoby automatycznie doklejoną pustą opcję ("- Select an
    # option -") w RadioSelect, mimo że ma tylko dwie sensowne wartości.
    lab_samples_delivered = forms.ChoiceField(
        choices=[('tak', 'Tak'), ('nie', 'Nie')], required=False,
        label='Czy dostarczono próbki do laboratorium?',
        widget=forms.RadioSelect(attrs={'class': 'status-radio'}),
    )

    def __init__(self, *args, production=None, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        team_fields = ('person_sd', 'person_qa', 'person_rd', 'person_te', 'person_ce', 'person_sl')
        if production is not None:
            for field_name in team_fields:
                self.initial[field_name] = getattr(production, field_name)
        _use_full_name_labels(self, *team_fields)
        _lock_team_fields_to_department(self, user, SENSORY_TEAM_FIELD_DEPTS)

    class Meta:
        model = ChecklistAfter
        # Linia pakująca nie jest już wpisywana tutaj - pochodzi z Etapu I
        # (ChecklistBeforeForm.packaging_line, zapisywane na
        # FirstProduction.packaging_line) i jest tylko wyświetlana.
        fields = [
            'production_date',
            'sample_start', 'sample_middle', 'sample_end',
            'comparison_benchmark', 'comparison_lab', 'comparison_reference',
            'yield_kg', 'yield_takty', 'lab_samples_delivered', 'uwagi',
            *_SIG_FIELD_NAMES, 'photo_sl',
        ]
        widgets = {
            **_SIG_WIDGETS,
            'photo_sl':       _SL_PHOTO_WIDGET,
            'yield_kg':       forms.TextInput(attrs={'class': 'form-control form-control-sm', 'placeholder': 'kg/h'}),
            'yield_takty':    forms.TextInput(attrs={'class': 'form-control form-control-sm', 'placeholder': 'takty'}),
            'uwagi':          forms.Textarea(attrs={'class': 'form-control form-control-sm', 'rows': 3}),
        }


# Krok 2 – pakowanie
class ChecklistAfterPackagingForm(forms.ModelForm):
    person_sd = _person_field('SD', 'SD')
    person_pp = _person_field('PP', 'PP')
    person_qa = _person_field('QA', 'QA')
    person_ql = _person_field('QL', 'QL')
    person_ce = _person_field('CE', 'CE')
    person_sl = _person_field('SL', 'Sprzedaż Lubeck')

    def __init__(self, *args, production=None, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        team_fields = ('person_sd', 'person_pp', 'person_qa', 'person_ql', 'person_ce', 'person_sl')
        if production is not None:
            # SD/QA/CE są zwykle już wybrane w sensoryce - dla powiązanej pary
            # sensoryka/pakowanie to osobne wiersze FirstProduction, więc bez
            # tego automatycznego przeniesienia pakowanie musiałoby wybierać je
            # jeszcze raz. Wynik jest tylko podpowiedzią - nadal można zmienić.
            source = production
            if not any(getattr(production, f) for f in ('person_sd', 'person_qa', 'person_ce')) and production.linked_production:
                source = production.linked_production
            for field_name in team_fields:
                value = getattr(production, field_name) or getattr(source, field_name)
                self.initial[field_name] = value
        _use_full_name_labels(self, *team_fields)
        _lock_team_fields_to_department(self, user, PACKAGING_TEAM_FIELD_DEPTS)

    class Meta:
        model = ChecklistAfter
        fields = [
            *_SIG_FIELD_NAMES, 'photo_sl',
            'photo_1', 'photo_2', 'photo_3', 'photo_4', 'umk_count',
        ]
        widgets = {
            **_SIG_WIDGETS,
            'photo_sl':  _SL_PHOTO_WIDGET,
            'umk_count': forms.TextInput(attrs={'class': 'form-control form-control-sm', 'placeholder': 'Liczba UMK'}),
            'photo_1': forms.FileInput(attrs={'class': 'form-control form-control-sm', 'accept': 'image/*'}),
            'photo_2': forms.FileInput(attrs={'class': 'form-control form-control-sm', 'accept': 'image/*'}),
            'photo_3': forms.FileInput(attrs={'class': 'form-control form-control-sm', 'accept': 'image/*'}),
            'photo_4': forms.FileInput(attrs={'class': 'form-control form-control-sm', 'accept': 'image/*'}),
        }


# Krok 3 – decyzja SD / zwolnienie
class ChecklistAfterAcceptanceForm(forms.ModelForm):
    decision = forms.ChoiceField(
        choices=ChecklistAfter.DECISION_CHOICES,
        label='Decyzja',
        widget=forms.RadioSelect(attrs={'class': 'decision-radio', 'id': 'id_decision'}),
    )

    class Meta:
        model = ChecklistAfter
        # Liczba UMK do śluzy nie jest już wpisywana tutaj - pochodzi z
        # Etapu I ("Wymagane dodatkowe próbki dla klienta? Ilość:") i jest
        # tylko wyświetlana (patrz checklist_after_acceptance.html).
        fields = [
            'photo_1', 'photo_2', 'photo_3', 'photo_4',
            'decision', 'conditional_comment',
            'correction_comment', 'correction_return_stage',
            'acceptance_date', 'acceptance_signature',
        ]
        widgets = {
            'acceptance_signature': forms.HiddenInput(),
            'acceptance_date':      forms.DateInput(format='%Y-%m-%d', attrs={'type': 'date', 'class': 'form-control'}),
            'conditional_comment':  forms.Textarea(attrs={'class': 'form-control form-control-sm', 'rows': 2,
                                                            'placeholder': 'Dlaczego akceptacja jest warunkowa?'}),
            'correction_comment':   forms.Textarea(attrs={'class': 'form-control form-control-sm', 'rows': 2,
                                                            'placeholder': 'Co należy poprawić?'}),
            'correction_return_stage': forms.Select(attrs={'class': 'form-select form-select-sm'}),
            'photo_1': forms.FileInput(attrs={'class': 'form-control form-control-sm', 'accept': 'image/*'}),
            'photo_2': forms.FileInput(attrs={'class': 'form-control form-control-sm', 'accept': 'image/*'}),
            'photo_3': forms.FileInput(attrs={'class': 'form-control form-control-sm', 'accept': 'image/*'}),
            'photo_4': forms.FileInput(attrs={'class': 'form-control form-control-sm', 'accept': 'image/*'}),
        }

    def clean(self):
        cleaned = super().clean()
        decision = cleaned.get('decision')
        if decision == 'conditional' and not cleaned.get('conditional_comment'):
            self.add_error('conditional_comment', 'Podaj powód akceptacji warunkowej.')
        if decision == 'correction':
            if not cleaned.get('correction_comment'):
                self.add_error('correction_comment', 'Podaj komentarz do korekty.')
            if not cleaned.get('correction_return_stage'):
                self.add_error('correction_return_stage', 'Wybierz etap powrotu.')
        return cleaned


# zachowane dla kompatybilności wstecznej (używane w widoku checklist_after)
ChecklistAfterHeaderForm = ChecklistAfterSensoryForm


class SensoryParamForm(forms.ModelForm):
    class Meta:
        model = SensoryParam
        fields = ['status', 'uwagi', 'korekta', 'kto', 'kiedy']
        widgets = {
            'status':  forms.RadioSelect(attrs={'class': 'status-radio'}),
            'uwagi':   forms.TextInput(attrs={'class': 'form-control form-control-sm'}),
            'korekta': forms.TextInput(attrs={'class': 'form-control form-control-sm'}),
            'kto':     forms.TextInput(attrs={'class': 'form-control form-control-sm'}),
            'kiedy':   forms.TextInput(attrs={'class': 'form-control form-control-sm'}),
        }


class PackagingItemForm(forms.ModelForm):
    class Meta:
        model = PackagingItem
        fields = ['status', 'uwagi', 'korekta', 'kto', 'kiedy']
        widgets = {
            'status':  forms.RadioSelect(attrs={'class': 'status-radio'}),
            'uwagi':   forms.TextInput(attrs={'class': 'form-control form-control-sm'}),
            'korekta': forms.TextInput(attrs={'class': 'form-control form-control-sm'}),
            'kto':     forms.TextInput(attrs={'class': 'form-control form-control-sm'}),
            'kiedy':   forms.TextInput(attrs={'class': 'form-control form-control-sm'}),
        }


SensoryParamFormSet = forms.modelformset_factory(SensoryParam, form=SensoryParamForm, extra=0)
PackagingItemFormSet = forms.modelformset_factory(PackagingItem, form=PackagingItemForm, extra=0)

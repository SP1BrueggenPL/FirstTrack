from django import forms
from django.contrib.auth.models import User
from .models import (
    FirstProduction, ChecklistBefore, ChecklistAfter,
    SensoryParam, PackagingItem, UserProfile, NotificationRecipient, DEPT_CHOICES,
)

# Pola "Szczegółowe informacje" (poza komentarzem) i "Numery" - dane podstawowe,
# które są obowiązkiem działu R&D: jeśli osobę otwierającą edycję przypisano do
# działu RD, te pola stają się wymagane przed zapisem. Dla innych działów zostają
# opcjonalne (mogą, ale nie muszą ich uzupełnić).
RD_REQUIRED_FIELDS = [
    'data_produkcji', 'zmiany', 'layout', 'typ_produkcji',
    'rd_number', 'recipe', 'crm_project_nr',
]

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
    email      = forms.EmailField(label='Email służbowy',
                                  widget=forms.EmailInput(attrs={'class': 'form-control'}))
    department = forms.ChoiceField(label='Dział', choices=[('', '– wybierz –')] + list(DEPT_CHOICES),
                                   widget=_sel('form-select'))
    chip_number = forms.CharField(label='Numer chip (5 cyfr)', widget=_CHIP_WIDGET)

    def clean_full_name(self):
        value = (self.cleaned_data.get('full_name') or '').strip()
        if len(value.split()) < 2:
            raise forms.ValidationError('Podaj imię i nazwisko.')
        return value

    def clean_email(self):
        email = self.cleaned_data['email']
        if User.objects.filter(email=email).exists():
            raise forms.ValidationError('Użytkownik z tym adresem email już istnieje.')
        return email

    def clean_chip_number(self):
        return _clean_chip_number(self.cleaned_data.get('chip_number'))

    def clean(self):
        cleaned = super().clean()
        if cleaned.get('full_name'):
            cleaned['first_name'], cleaned['last_name'] = split_full_name(cleaned['full_name'])
        return cleaned


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
        value = (self.cleaned_data.get('full_name') or '').strip()
        if len(value.split()) < 2:
            raise forms.ValidationError('Podaj imię i nazwisko.')
        return value

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


class FirstProductionForm(forms.ModelForm):
    data_produkcji = forms.DateField(
        required=False,
        input_formats=['%Y-%m-%d'],
        widget=forms.DateInput(format='%Y-%m-%d', attrs={'type': 'date', 'class': 'form-control'}),
        label='Data produkcji',
    )

    class Meta:
        model = FirstProduction
        fields = [
            'sap_zlecenie', 'sap_material', 'product_name',
            'scope', 'data_produkcji', 'zmiany', 'layout', 'typ_produkcji', 'komentarz',
            'fert_number', 'rd_number', 'recipe', 'crm_project_nr',
            'person_rd', 'person_sc', 'person_ql', 'person_qa',
            'person_sd', 'person_pp', 'person_ce', 'person_te',
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
            'person_rd':      forms.Select(attrs={'class': 'form-select form-select-sm person-select'}),
            'person_sc':      forms.Select(attrs={'class': 'form-select form-select-sm person-select'}),
            'person_ql':      forms.Select(attrs={'class': 'form-select form-select-sm person-select'}),
            'person_qa':      forms.Select(attrs={'class': 'form-select form-select-sm person-select'}),
            'person_sd':      forms.Select(attrs={'class': 'form-select form-select-sm person-select', 'id': 'id_person_sd'}),
            'person_pp':      forms.Select(attrs={'class': 'form-select form-select-sm person-select'}),
            'person_ce':      forms.Select(attrs={'class': 'form-select form-select-sm person-select'}),
            'person_te':      forms.Select(attrs={'class': 'form-select form-select-sm person-select'}),
            'acceptor':       forms.Select(attrs={'class': 'form-select', 'id': 'id_acceptor'}),
        }

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        # Ogranicz listy do właściwych działów
        depts = {
            'person_rd': 'RD', 'person_sc': 'SC', 'person_ql': 'QL',
            'person_qa': 'QA', 'person_sd': 'SD', 'person_pp': 'PP',
            'person_ce': 'CE', 'person_te': 'TE',
        }
        for field_name, dept in depts.items():
            self.fields[field_name].queryset = (
                User.objects.filter(profile__department=dept)
                            .select_related('profile')
                            .order_by('last_name', 'first_name')
            )
            self.fields[field_name].empty_label = '– wybierz –'
            self.fields[field_name].required = False

        self.fields['acceptor'].queryset = (
            User.objects.filter(profile__department='SD')
                        .select_related('profile')
                        .order_by('last_name', 'first_name')
        )
        self.fields['acceptor'].empty_label = '– wybierz akceptującego –'
        self.fields['acceptor'].required = False

        # Osoba z działu R&D musi uzupełnić wszystkie dane podstawowe (poza
        # komentarzem) przed zapisem - dla innych działów pola zostają opcjonalne.
        profile = getattr(user, 'profile', None) if user is not None else None
        if profile and profile.department == 'RD':
            for field_name in RD_REQUIRED_FIELDS:
                self.fields[field_name].required = True

    def clean_fert_number(self):
        value = self.cleaned_data.get('fert_number', '')
        if self.cleaned_data.get('scope') == 'packaging' and not value:
            raise forms.ValidationError('Numer FERT jest wymagany dla produkcji „tylko pakowanie".')
        return value

    def clean_recipe(self):
        value = self.cleaned_data.get('recipe', '')
        if self.cleaned_data.get('scope') in ('packaging', 'sensory') and not value:
            raise forms.ValidationError('Numer receptury jest wymagany dla tego zakresu produkcji.')
        return value

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
    class Meta:
        model = ChecklistBefore
        fields = '__all__'
        exclude = ['production', 'completed_at', 'created_at', 'updated_at']
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
            'test_packaging_1_name':   forms.TextInput(attrs={'class': 'form-control form-control-sm', 'placeholder': 'Nazwa pozycji'}),
            'test_packaging_1_nadzor': forms.TextInput(attrs={'class': 'form-control form-control-sm', 'placeholder': 'Nadzór'}),
            'test_packaging_2_name':   forms.TextInput(attrs={'class': 'form-control form-control-sm', 'placeholder': 'Nazwa pozycji'}),
            'test_packaging_2_nadzor': forms.TextInput(attrs={'class': 'form-control form-control-sm', 'placeholder': 'Nadzór'}),
            'test_packaging_3_name':   forms.TextInput(attrs={'class': 'form-control form-control-sm', 'placeholder': 'Nazwa pozycji'}),
            'test_packaging_3_nadzor': forms.TextInput(attrs={'class': 'form-control form-control-sm', 'placeholder': 'Nadzór'}),
            'confirm_rd':  forms.TextInput(attrs={'class': 'form-control form-control-sm'}),
            'confirm_pp':  forms.TextInput(attrs={'class': 'form-control form-control-sm'}),
            'confirm_ce':  forms.TextInput(attrs={'class': 'form-control form-control-sm'}),
            'confirm_qa':  forms.TextInput(attrs={'class': 'form-control form-control-sm'}),
            'confirm_sd':  forms.TextInput(attrs={'class': 'form-control form-control-sm'}),
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

# Krok 1 – parametry sensoryczne
class ChecklistAfterSensoryForm(forms.ModelForm):
    production_date = forms.DateField(
        required=False,
        input_formats=['%Y-%m-%d'],
        widget=forms.DateInput(format='%Y-%m-%d', attrs={'type': 'date', 'class': 'form-control form-control-sm'}),
        label='Data produkcji',
    )

    class Meta:
        model = ChecklistAfter
        fields = [
            'packaging_line', 'production_date',
            'sample_start', 'sample_middle', 'sample_end',
            'comparison_benchmark', 'comparison_lab', 'comparison_reference',
            'yield_kg', 'yield_takty', 'uwagi',
            'sig_rd', 'sig_sc', 'sig_ql', 'sig_qa', 'sig_sd', 'sig_pp', 'sig_ce', 'sig_te',
        ]
        widgets = {
            **_SIG_WIDGETS,
            'packaging_line': forms.TextInput(attrs={'class': 'form-control form-control-sm'}),
            'yield_kg':       forms.TextInput(attrs={'class': 'form-control form-control-sm', 'placeholder': 'kg/h'}),
            'yield_takty':    forms.TextInput(attrs={'class': 'form-control form-control-sm', 'placeholder': 'takty'}),
            'uwagi':          forms.Textarea(attrs={'class': 'form-control form-control-sm', 'rows': 3}),
        }


# Krok 2 – pakowanie
class ChecklistAfterPackagingForm(forms.ModelForm):
    class Meta:
        model = ChecklistAfter
        fields = [
            'sig_rd', 'sig_sc', 'sig_ql', 'sig_qa', 'sig_sd', 'sig_pp', 'sig_ce', 'sig_te',
            'photo_1', 'photo_2', 'photo_3', 'photo_4', 'umk_count',
        ]
        widgets = {
            **_SIG_WIDGETS,
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
        fields = [
            'photo_1', 'photo_2', 'photo_3', 'photo_4', 'umk_count',
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
            'umk_count': forms.TextInput(attrs={'class': 'form-control form-control-sm', 'placeholder': 'Liczba UMK'}),
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

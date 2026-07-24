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


class UserCreateForm(forms.Form):
    first_name = forms.CharField(label='Imię', max_length=150,
                                 widget=_fc('Imię', 'form-control'))
    last_name  = forms.CharField(label='Nazwisko', max_length=150,
                                 widget=_fc('Nazwisko', 'form-control'))
    email      = forms.EmailField(label='Email służbowy',
                                  widget=forms.EmailInput(attrs={'class': 'form-control'}))
    department = forms.ChoiceField(label='Dział', choices=[('', '– wybierz –')] + list(DEPT_CHOICES),
                                   widget=_sel('form-select'))
    phone      = forms.CharField(label='Telefon', max_length=30, required=False,
                                 widget=_fc('np. +48 500 000 000', 'form-control'))
    chip_number = forms.CharField(label='Numer chip (5 cyfr)', widget=_CHIP_WIDGET)

    def clean_email(self):
        email = self.cleaned_data['email']
        if User.objects.filter(email=email).exists():
            raise forms.ValidationError('Użytkownik z tym adresem email już istnieje.')
        return email

    def clean_chip_number(self):
        chip = self.cleaned_data['chip_number'].strip()
        if not chip.isdigit() or len(chip) != 5:
            raise forms.ValidationError('Numer chip musi składać się z dokładnie 5 cyfr.')
        if UserProfile.objects.filter(chip_number=chip).exists():
            raise forms.ValidationError('Ten numer chip jest już przypisany do innego użytkownika.')
        return chip


class UserEditForm(forms.Form):
    first_name = forms.CharField(label='Imię', max_length=150,
                                 widget=_fc('Imię', 'form-control'))
    last_name  = forms.CharField(label='Nazwisko', max_length=150,
                                 widget=_fc('Nazwisko', 'form-control'))
    email      = forms.EmailField(label='Email służbowy',
                                  widget=forms.EmailInput(attrs={'class': 'form-control'}))
    department = forms.ChoiceField(label='Dział', choices=[('', '– wybierz –')] + list(DEPT_CHOICES),
                                   widget=_sel('form-select'))
    phone      = forms.CharField(label='Telefon', max_length=30, required=False,
                                 widget=_fc('np. +48 500 000 000', 'form-control'))
    is_active  = forms.BooleanField(label='Konto aktywne', required=False)
    is_staff   = forms.BooleanField(label='Admin', required=False)


class UserChipForm(forms.Form):
    chip_number = forms.CharField(label='Nowy numer chip (5 cyfr)', widget=_CHIP_WIDGET)

    def __init__(self, *args, user=None, **kwargs):
        self.user = user
        super().__init__(*args, **kwargs)

    def clean_chip_number(self):
        chip = self.cleaned_data['chip_number'].strip()
        if not chip.isdigit() or len(chip) != 5:
            raise forms.ValidationError('Numer chip musi składać się z dokładnie 5 cyfr.')
        qs = UserProfile.objects.filter(chip_number=chip)
        if self.user is not None:
            qs = qs.exclude(user=self.user)
        if qs.exists():
            raise forms.ValidationError('Ten numer chip jest już przypisany do innego użytkownika.')
        return chip


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
            'data_produkcji', 'zmiany', 'layout', 'typ_produkcji', 'komentarz',
            'packaging_line', 'rd_number', 'recipe', 'crm_project_nr',
            'person_rd', 'person_sc', 'person_ql', 'person_qa',
            'person_sd', 'person_sdp', 'person_pp', 'person_ce',
            'acceptor', 'acceptor_email',
        ]
        widgets = {
            'sap_zlecenie':   _fc('np. 11333525'),
            'sap_material':   _fc('np. 28124'),
            'product_name':   forms.TextInput(attrs={'class': 'form-control'}),
            'data_produkcji': _date('form-control'),
            'zmiany':         _fc('np. nowy indeks, new article/new line'),
            'layout':         _fc('np. BMC, ND'),
            'typ_produkcji':  _sel('form-select form-select-sm'),
            'komentarz':      forms.Textarea(attrs={'class': 'form-control form-control-sm', 'rows': 2}),
            'packaging_line': _fc(),
            'rd_number':      _fc(),
            'recipe':         _fc(),
            'crm_project_nr': _fc(),
            'person_rd':      forms.Select(attrs={'class': 'form-select form-select-sm person-select'}),
            'person_sc':      forms.Select(attrs={'class': 'form-select form-select-sm person-select'}),
            'person_ql':      forms.Select(attrs={'class': 'form-select form-select-sm person-select'}),
            'person_qa':      forms.Select(attrs={'class': 'form-select form-select-sm person-select'}),
            'person_sd':      forms.Select(attrs={'class': 'form-select form-select-sm person-select', 'id': 'id_person_sd'}),
            'person_sdp':     forms.Select(attrs={'class': 'form-select form-select-sm person-select'}),
            'person_pp':      forms.Select(attrs={'class': 'form-select form-select-sm person-select'}),
            'person_ce':      forms.Select(attrs={'class': 'form-select form-select-sm person-select'}),
            'acceptor':       forms.Select(attrs={'class': 'form-select', 'id': 'id_acceptor'}),
            'acceptor_email': forms.EmailInput(attrs={'class': 'form-control', 'id': 'id_acceptor_email',
                                                       'placeholder': 'Auto-uzupełniany z konta'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Ogranicz listy do właściwych działów
        depts = {
            'person_rd': 'RD', 'person_sc': 'SC', 'person_ql': 'QL',
            'person_qa': 'QA', 'person_sd': 'SD', 'person_sdp': 'SDP',
            'person_pp': 'PP', 'person_ce': 'CE',
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

    def _user_label(self, user):
        return user.get_full_name() or user.username


class SAPImportForm(forms.Form):
    screenshot = forms.ImageField(
        label='Screenshot z SAP lub tabeli planowania',
        help_text='Obsługiwane: JPG, PNG, BMP, WEBP',
        widget=forms.FileInput(attrs={'class': 'form-control', 'accept': 'image/*'}),
    )


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
            'confirm_sdp': forms.TextInput(attrs={'class': 'form-control form-control-sm'}),
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
    'sig_sdp': forms.HiddenInput(),
    'sig_pp':  forms.HiddenInput(),
    'sig_ce':  forms.HiddenInput(),
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
            'sig_rd', 'sig_sc', 'sig_ql', 'sig_qa', 'sig_sd', 'sig_sdp', 'sig_pp', 'sig_ce',
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
            'sig_rd', 'sig_sc', 'sig_ql', 'sig_qa', 'sig_sd', 'sig_sdp', 'sig_pp', 'sig_ce',
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


# Krok 3 – akceptacja SD
class ChecklistAfterAcceptanceForm(forms.ModelForm):
    class Meta:
        model = ChecklistAfter
        fields = [
            'photo_1', 'photo_2', 'photo_3', 'photo_4', 'umk_count',
            'final_acceptance', 'acceptance_date', 'acceptance_signature',
        ]
        widgets = {
            'acceptance_signature': forms.HiddenInput(),
            'acceptance_date':      forms.DateInput(format='%Y-%m-%d', attrs={'type': 'date', 'class': 'form-control'}),
            'final_acceptance':     forms.NullBooleanSelect(attrs={'class': 'form-select'}),
            'umk_count': forms.TextInput(attrs={'class': 'form-control form-control-sm', 'placeholder': 'Liczba UMK'}),
            'photo_1': forms.FileInput(attrs={'class': 'form-control form-control-sm', 'accept': 'image/*'}),
            'photo_2': forms.FileInput(attrs={'class': 'form-control form-control-sm', 'accept': 'image/*'}),
            'photo_3': forms.FileInput(attrs={'class': 'form-control form-control-sm', 'accept': 'image/*'}),
            'photo_4': forms.FileInput(attrs={'class': 'form-control form-control-sm', 'accept': 'image/*'}),
        }


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

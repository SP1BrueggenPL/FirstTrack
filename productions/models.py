from django.core.validators import RegexValidator
from django.db import models
from django.contrib.auth.models import User
from django.db.models.signals import post_save
from django.dispatch import receiver

ITEM_STATUS = [('tak', 'Tak'), ('nie', 'Nie'), ('nd', 'Nie dotyczy'), ('', '-')]

chip_number_validator = RegexValidator(r'^\d{5}$', 'Numer chip musi składać się z dokładnie 5 cyfr.')

DEPT_CHOICES = [
    ('RD',  'R&D'),
    ('SC',  'SC'),
    ('QL',  'QL'),
    ('QA',  'QA'),
    ('SD',  'SD'),
    ('WPD', 'WPD'),
    ('PP',  'PP'),
    ('CE',  'CE'),
    ('TE',  'Technologia'),
]


# ──────────────────────────────────────────────────────────
# Profil użytkownika (dział)
# ──────────────────────────────────────────────────────────

class UserProfile(models.Model):
    user        = models.OneToOneField(User, on_delete=models.CASCADE, related_name='profile')
    department  = models.CharField('Dział', max_length=10, choices=DEPT_CHOICES, blank=True)
    chip_number = models.CharField(
        'Numer chip', max_length=5, unique=True, null=True, blank=True,
        validators=[chip_number_validator],
        help_text='5-cyfrowy numer używany do logowania (zamiast hasła).',
    )

    class Meta:
        verbose_name = 'Profil użytkownika'
        verbose_name_plural = 'Profile użytkowników'
        ordering = ['department', 'user__last_name']

    def __str__(self):
        name = self.user.get_full_name() or self.user.username
        dept = self.get_department_display()
        return f"{name} ({dept})" if dept else name


@receiver(post_save, sender=User)
def _ensure_profile(sender, instance, created, **kwargs):
    if created:
        UserProfile.objects.get_or_create(user=instance)


# ──────────────────────────────────────────────────────────
# Helper: FK do użytkownika z danego działu
# ──────────────────────────────────────────────────────────

def _person_fk(dept_code, related, label):
    return models.ForeignKey(
        User,
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name=related,
        verbose_name=label,
    )


# ──────────────────────────────────────────────────────────
# Pierwsza Produkcja
# ──────────────────────────────────────────────────────────

class FirstProduction(models.Model):
    STATUS_CHOICES = [
        ('nowa',      'Nowa'),
        ('etap1',     'Etap I'),
        ('etap2',     'Etap II – Produkcja'),
        ('etap3',     'Etap III – Test pakowania'),
        ('zwolniona', 'Zwolniona do sprzedaży'),
    ]
    TYP_CHOICES = [('A', 'A'), ('B', 'B'), ('', '–')]

    SCOPE_CHOICES = [
        ('full',      'Produkcja z sensoryką i pakowaniem'),
        ('sensory',   'Tylko produkcja (sensoryka)'),
        ('packaging', 'Tylko pakowanie'),
    ]

    # ── Dane z SAP (ekstrakcja AI ze zrzutu) ─────────────
    sap_zlecenie = models.CharField('Zlecenie SAP', max_length=20, blank=True)
    sap_material = models.CharField('Nr materiału SAP', max_length=20, blank=True)
    product_name = models.CharField('Krótki tekst materiału', max_length=300)

    # ── Szczegółowe informacje ────────────────────────────
    scope          = models.CharField('Zakres produkcji', max_length=10,
                                      choices=SCOPE_CHOICES, default='full')
    data_produkcji = models.DateField('Data produkcji', null=True, blank=True)
    zmiany         = models.CharField('Zmiany', max_length=300, blank=True)
    layout         = models.CharField('Layout', max_length=20, blank=True)
    typ_produkcji  = models.CharField('Typ produkcji A/B', max_length=1,
                                      choices=TYP_CHOICES, blank=True)
    komentarz      = models.TextField('Komentarz', blank=True)
    fert_number    = models.CharField('Numer FERT', max_length=50, blank=True)

    # ── Numery ─────────────────────────────────────────────
    packaging_line = models.CharField('Linia pakująca', max_length=50, blank=True)
    rd_number      = models.CharField('Nr receptury R&D', max_length=50, blank=True)
    recipe         = models.CharField('Receptura powiązana', max_length=100, blank=True)
    crm_project_nr = models.CharField('CRM Projekt Nr.', max_length=50, blank=True)

    # ── Powiązanie sensoryka ↔ pakowanie (produkcje z rozdzielonym zakresem) ──
    linked_production = models.ForeignKey(
        'self', null=True, blank=True, on_delete=models.SET_NULL, related_name='+',
        verbose_name='Powiązana produkcja',
    )
    linked_at = models.DateTimeField('Data powiązania', null=True, blank=True)
    linked_by = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.SET_NULL, related_name='+',
        verbose_name='Powiązano przez',
    )

    # ── Zespół (FK → User) ────────────────────────────────
    person_rd  = _person_fk('RD',  'prod_rd',  'R&D')
    person_sc  = _person_fk('SC',  'prod_sc',  'SC')
    person_ql  = _person_fk('QL',  'prod_ql',  'QL')
    person_qa  = _person_fk('QA',  'prod_qa',  'QA')
    person_sd  = _person_fk('SD',  'prod_sd',  'SD')
    person_wpd = _person_fk('WPD', 'prod_wpd', 'WPD')
    person_pp  = _person_fk('PP',  'prod_pp',  'PP')
    person_ce  = _person_fk('CE',  'prod_ce',  'CE')
    person_te  = _person_fk('TE',  'prod_te',  'Technologia')

    # ── Akceptacja / email ────────────────────────────────
    acceptor       = _person_fk('SD', 'prod_acceptor', 'Osoba akceptująca (SD)')
    acceptor_email = models.EmailField('Email akceptującego', blank=True)

    # ── Status i metadane ─────────────────────────────────
    status         = models.CharField('Status', max_length=20,
                                      choices=STATUS_CHOICES, default='nowa')
    sap_screenshot = models.ImageField('Screenshot SAP',
                                       upload_to='sap_screenshots/', blank=True, null=True)
    email_sent     = models.BooleanField('Mail wysłany', default=False)
    email_sent_at  = models.DateTimeField('Data wysłania maila', null=True, blank=True)
    reminder_sent_at = models.DateTimeField('Data wysłania przypomnienia', null=True, blank=True)
    created_at     = models.DateTimeField(auto_now_add=True)
    updated_at     = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-data_produkcji', '-created_at']
        verbose_name = 'Pierwsza Produkcja'
        verbose_name_plural = 'Pierwsze Produkcje'

    def __str__(self):
        return f"{self.sap_zlecenie or self.sap_material or '—'} | {self.product_name}"

    def get_status_badge(self):
        colors = {
            'nowa': 'secondary', 'etap1': 'info', 'etap2': 'warning',
            'etap3': 'primary', 'zwolniona': 'success',
        }
        return colors.get(self.status, 'secondary')

    @property
    def is_sensory_only(self):
        return self.scope == 'sensory'

    @property
    def is_packaging_only(self):
        return self.scope == 'packaging'

    @property
    def skips_sensory(self):
        return self.scope == 'packaging'


# ──────────────────────────────────────────────────────────
# Checklista PRZED
# ──────────────────────────────────────────────────────────

class ChecklistBefore(models.Model):
    production = models.OneToOneField(
        FirstProduction, on_delete=models.CASCADE, related_name='checklist_before'
    )
    order_updated_status  = models.CharField(max_length=3, choices=ITEM_STATUS, blank=True)
    order_updated_uwagi   = models.TextField(blank=True)
    pwpr_status           = models.CharField(max_length=3, choices=ITEM_STATUS, blank=True)
    pwpr_uwagi            = models.TextField(blank=True)
    analysis_form_status  = models.CharField(max_length=3, choices=ITEM_STATUS, blank=True)
    analysis_form_version = models.CharField('Wersja dokumentu', max_length=50, blank=True)
    zero_sample_status    = models.CharField(max_length=3, choices=ITEM_STATUS, blank=True)
    zero_sample_uwagi     = models.TextField(blank=True)
    production_card_status  = models.CharField(max_length=3, choices=ITEM_STATUS, blank=True)
    production_card_uwagi   = models.TextField(blank=True)
    machine_suitable_status = models.CharField(max_length=3, choices=ITEM_STATUS, blank=True)
    machine_suitable_uwagi  = models.TextField(blank=True)
    packaging_layout_status = models.CharField(max_length=3, choices=ITEM_STATUS, blank=True)
    packaging_layout_uwagi  = models.TextField(blank=True)
    collective_label_status = models.CharField(max_length=3, choices=ITEM_STATUS, blank=True)
    collective_label_uwagi  = models.TextField(blank=True)
    date_format_status    = models.CharField(max_length=3, choices=ITEM_STATUS, blank=True)
    date_format_uwagi     = models.TextField(blank=True)
    bom_set_status        = models.CharField(max_length=3, choices=ITEM_STATUS, blank=True)
    bom_set_uwagi         = models.TextField(blank=True)
    planned_yield_kg      = models.CharField('Planowana wydajność kg/h', max_length=50, blank=True)
    planned_yield_takty   = models.CharField('Takty', max_length=50, blank=True)
    additional_samples_status = models.CharField(max_length=3, choices=ITEM_STATUS, blank=True)
    additional_samples_count  = models.CharField('Ilość próbek', max_length=20, blank=True)
    test_packaging_1_name   = models.CharField(max_length=200, blank=True)
    test_packaging_1_status = models.CharField(max_length=3, choices=ITEM_STATUS, blank=True)
    test_packaging_1_nadzor = models.CharField(max_length=100, blank=True)
    test_packaging_2_name   = models.CharField(max_length=200, blank=True)
    test_packaging_2_status = models.CharField(max_length=3, choices=ITEM_STATUS, blank=True)
    test_packaging_2_nadzor = models.CharField(max_length=100, blank=True)
    test_packaging_3_name   = models.CharField(max_length=200, blank=True)
    test_packaging_3_status = models.CharField(max_length=3, choices=ITEM_STATUS, blank=True)
    test_packaging_3_nadzor = models.CharField(max_length=100, blank=True)
    confirm_rd  = models.CharField('Podpis R&D', max_length=100, blank=True)
    confirm_pp  = models.CharField('Podpis PP',  max_length=100, blank=True)
    confirm_ce  = models.CharField('Podpis CE',  max_length=100, blank=True)
    confirm_qa  = models.CharField('Podpis QA',  max_length=100, blank=True)
    confirm_wpd = models.CharField('Podpis WPD', max_length=100, blank=True)
    confirm_sd  = models.CharField('Podpis SD',  max_length=100, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at   = models.DateTimeField(auto_now_add=True)
    updated_at   = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Checklista Przed'

    def __str__(self):
        return f"Checklista przed – {self.production}"

    def is_complete(self):
        return bool(self.confirm_sd and self.confirm_qa)


# ──────────────────────────────────────────────────────────
# Checklista PO
# ──────────────────────────────────────────────────────────

class ChecklistAfter(models.Model):
    production      = models.OneToOneField(
        FirstProduction, on_delete=models.CASCADE, related_name='checklist_after'
    )
    production_line = models.CharField('Linia prod.', max_length=50, blank=True)
    packaging_line  = models.CharField('Linia pakuj.', max_length=50, blank=True)
    production_date = models.DateField('Data produkcji', null=True, blank=True)
    person_rd  = models.CharField('R&D',  max_length=100, blank=True)
    person_sc  = models.CharField('SC',   max_length=100, blank=True)
    person_ql  = models.CharField('QL',   max_length=100, blank=True)
    person_qa  = models.CharField('QA',   max_length=100, blank=True)
    person_sd  = models.CharField('SD',   max_length=100, blank=True)
    person_wpd = models.CharField('WPD',  max_length=100, blank=True)
    person_pp  = models.CharField('PP',   max_length=100, blank=True)
    person_ce  = models.CharField('CE',   max_length=100, blank=True)
    person_te  = models.CharField('Technologia', max_length=100, blank=True)
    sample_start  = models.BooleanField('Próbka – Początek', default=False)
    sample_middle = models.BooleanField('Próbka – Środek',   default=False)
    sample_end    = models.BooleanField('Próbka – Koniec',   default=False)
    comparison_benchmark = models.BooleanField('Benchmark',         default=False)
    comparison_lab       = models.BooleanField('Próbka laboratoryjna', default=False)
    comparison_reference = models.BooleanField('Próbka wzorcowa',   default=False)
    yield_kg    = models.CharField('Uzyskana wydajność kg/h', max_length=50, blank=True)
    yield_takty = models.CharField('Takty', max_length=50, blank=True)
    uwagi       = models.TextField('Uwagi', blank=True)

    DECISION_CHOICES = [
        ('accept',      'Akceptacja'),
        ('conditional', 'Akceptacja warunkowa'),
        ('correction',  'Do korekty'),
    ]
    RETURN_STAGE_CHOICES = [
        ('sensory',   'Sensoryka'),
        ('packaging', 'Pakownia'),
    ]
    decision             = models.CharField('Decyzja', max_length=15,
                                             choices=DECISION_CHOICES, blank=True)
    conditional_comment  = models.TextField('Komentarz (akceptacja warunkowa)', blank=True)
    correction_comment   = models.TextField('Komentarz (do korekty)', blank=True)
    correction_return_stage = models.CharField('Powrót do etapu', max_length=10,
                                               choices=RETURN_STAGE_CHOICES, blank=True)

    final_acceptance   = models.BooleanField('Akceptacja SD', null=True)
    acceptance_date    = models.DateField('Data akceptacji', null=True, blank=True)
    acceptance_signature = models.TextField('Podpis SD', blank=True)
    sig_rd  = models.TextField('Podpis R&D',  blank=True)
    sig_sc  = models.TextField('Podpis SC',   blank=True)
    sig_ql  = models.TextField('Podpis QL',   blank=True)
    sig_qa  = models.TextField('Podpis QA',   blank=True)
    sig_sd  = models.TextField('Podpis SD',   blank=True)
    sig_wpd = models.TextField('Podpis WPD',  blank=True)
    sig_pp  = models.TextField('Podpis PP',   blank=True)
    sig_ce  = models.TextField('Podpis CE',   blank=True)
    sig_te  = models.TextField('Podpis Technologia', blank=True)
    photo_1 = models.ImageField(upload_to='production_photos/', blank=True, null=True)
    photo_2 = models.ImageField(upload_to='production_photos/', blank=True, null=True)
    photo_3 = models.ImageField(upload_to='production_photos/', blank=True, null=True)
    photo_4 = models.ImageField(upload_to='production_photos/', blank=True, null=True)
    umk_count    = models.CharField('Liczba UMK do śluzy', max_length=50, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at   = models.DateTimeField(auto_now_add=True)
    updated_at   = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Checklista Po'

    def __str__(self):
        return f"Checklista po – {self.production}"

    def save(self, *args, **kwargs):
        is_new = self.pk is None
        super().save(*args, **kwargs)
        if is_new:
            self._create_default_params()

    def _create_default_params(self):
        for key, _ in SensoryParam.PARAM_CHOICES:
            SensoryParam.objects.create(checklist=self, param=key)
        for key, _ in PackagingItem.ITEM_CHOICES:
            PackagingItem.objects.create(checklist=self, item=key)


class SensoryParam(models.Model):
    PARAM_CHOICES = [
        ('smak',     'Smak'),
        ('zapach',   'Zapach'),
        ('wyglad',   'Wygląd'),
        ('kolor',    'Kolor'),
        ('tekstura', 'Tekstura'),
        ('gestosc',  'Gęstość usypowa'),
    ]
    checklist = models.ForeignKey(ChecklistAfter, on_delete=models.CASCADE, related_name='sensory_params')
    param  = models.CharField(max_length=20, choices=PARAM_CHOICES)
    status = models.CharField(max_length=3, choices=ITEM_STATUS, blank=True)
    uwagi  = models.TextField(blank=True)
    korekta = models.TextField(blank=True)
    kto    = models.CharField(max_length=100, blank=True)
    kiedy  = models.CharField(max_length=100, blank=True)

    class Meta:
        ordering = ['id']


class PackagingItem(models.Model):
    ITEM_CHOICES = [
        ('ean',            'EAN'),
        ('etykieta_umk',   'Etykieta UMK'),
        ('zdjecie_produkt','Zdjęcie + Produkt'),
        ('zamkniecie',     'Zamknięcie'),
        ('logo',           'Logo (aktualne, poprawne)'),
        ('sklad_receptury','Skład receptury'),
        ('mhd_pd',         'MHD Druck / PD Druck'),
        ('wypelnienie',    'Wypełnienie'),
    ]
    checklist = models.ForeignKey(ChecklistAfter, on_delete=models.CASCADE, related_name='packaging_items')
    item   = models.CharField(max_length=30, choices=ITEM_CHOICES)
    status = models.CharField(max_length=3, choices=ITEM_STATUS, blank=True)
    uwagi  = models.TextField(blank=True)
    korekta = models.TextField(blank=True)
    kto    = models.CharField(max_length=100, blank=True)
    kiedy  = models.CharField(max_length=100, blank=True)

    class Meta:
        ordering = ['id']


class EmailLog(models.Model):
    production = models.ForeignKey(FirstProduction, on_delete=models.CASCADE, related_name='email_logs',
                                   null=True, blank=True)
    recipient  = models.CharField(max_length=200)
    subject    = models.CharField(max_length=300)
    body       = models.TextField()
    sent_at    = models.DateTimeField(auto_now_add=True)
    success    = models.BooleanField(default=True)
    error_msg  = models.TextField(blank=True)

    class Meta:
        ordering = ['-sent_at']

    def __str__(self):
        return f"Mail do {self.recipient} – {self.sent_at:%Y-%m-%d %H:%M}"


# ──────────────────────────────────────────────────────────
# Stała pula odbiorców maili o pierwszej produkcji
# ──────────────────────────────────────────────────────────

class NotificationRecipient(models.Model):
    email      = models.EmailField('Adres email', unique=True)
    label      = models.CharField('Opis', max_length=100, blank=True)
    active     = models.BooleanField('Aktywny', default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['email']
        verbose_name = 'Stały odbiorca (pierwsza produkcja)'
        verbose_name_plural = 'Stali odbiorcy (pierwsza produkcja)'

    def __str__(self):
        return f"{self.email} ({self.label})" if self.label else self.email

import base64
import json
from datetime import date
from unittest import mock

from django.contrib.auth.models import User
from django.core import mail
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings

from .models import ChecklistBefore, EmailLog, EmailSettings, FirstProduction, UserProfile

_1PX_PNG = base64.b64decode(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=')


def _make_user(username, dept, email=None):
    user = User.objects.create_user(
        username=username, password='x', first_name=username.title(), last_name=dept,
        email=email or f'{username}@example.com',
    )
    user.profile.department = dept
    user.profile.save()
    return user


class FirstProductionFormRequiredFieldsTests(TestCase):
    """Pola "Szczegółowe informacje"/"Numery" (poza krótkim tekstem materiału)
    są opcjonalne dla każdego działu i każdego zakresu produkcji - produkcję
    można zapisać z niekompletnymi danymi i uzupełnić je później."""

    def setUp(self):
        self.rd = _make_user('rduser', 'RD')
        self.sd = _make_user('sduser', 'SD')

    def test_rd_can_save_with_minimal_data(self):
        self.client.force_login(self.rd)
        resp = self.client.post('/nowa/', {
            'sap_zlecenie': '1', 'sap_material': '2', 'product_name': 'X', 'scope': 'full',
        })
        self.assertEqual(resp.status_code, 302, resp.context['form'].errors if resp.status_code == 200 else None)
        self.assertTrue(FirstProduction.objects.filter(sap_zlecenie='1').exists())

    def test_other_department_fields_stay_optional(self):
        self.client.force_login(self.sd)
        resp = self.client.post('/nowa/', {
            'sap_zlecenie': '1', 'sap_material': '2', 'product_name': 'X', 'scope': 'full',
        })
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(FirstProduction.objects.filter(sap_zlecenie='1').exists())

    def test_packaging_only_does_not_require_fert_or_recipe(self):
        self.client.force_login(self.sd)
        resp = self.client.post('/nowa/', {
            'sap_zlecenie': '1', 'sap_material': '2', 'product_name': 'X', 'scope': 'packaging',
        })
        self.assertEqual(resp.status_code, 302, resp.context['form'].errors if resp.status_code == 200 else None)
        self.assertTrue(FirstProduction.objects.filter(sap_zlecenie='1').exists())

    def test_sensory_only_does_not_require_recipe(self):
        self.client.force_login(self.sd)
        resp = self.client.post('/nowa/', {
            'sap_zlecenie': '1', 'sap_material': '2', 'product_name': 'X', 'scope': 'sensory',
        })
        self.assertEqual(resp.status_code, 302, resp.context['form'].errors if resp.status_code == 200 else None)
        self.assertTrue(FirstProduction.objects.filter(sap_zlecenie='1').exists())


class ScopeRoutingAndLinkingTests(TestCase):
    """Produkcje 'tylko pakowanie' pomijają etap sensoryczny; produkcje 'tylko
    sensoryka' mogą powiązać się ze zleceniem pakowania po numerze SAP."""

    def setUp(self):
        self.sd = _make_user('sduser', 'SD')
        self.client.force_login(self.sd)
        self.sensory = FirstProduction.objects.create(
            sap_zlecenie='S1', product_name='Sensory', scope='sensory')
        self.packaging = FirstProduction.objects.create(
            sap_zlecenie='P1', product_name='Packaging', scope='packaging',
            fert_number='F1', recipe='R1')

    def test_packaging_only_skips_sensory_step(self):
        resp = self.client.get(f'/{self.packaging.pk}/etap2/')
        self.assertRedirects(resp, f'/{self.packaging.pk}/etap2/pakowanie/')

    def test_sensory_only_enters_sensory_step(self):
        resp = self.client.get(f'/{self.sensory.pk}/etap2/')
        self.assertRedirects(resp, f'/{self.sensory.pk}/etap2/sensoryczne/')

    def test_link_is_reciprocal(self):
        # touch the sensory checklist so it exists before linking
        self.client.get(f'/{self.sensory.pk}/etap2/sensoryczne/')
        resp = self.client.post(f'/{self.sensory.pk}/etap2/powiaz-pakowanie/',
                                 {'packaging_production': self.packaging.pk})
        self.assertEqual(resp.status_code, 302)
        self.sensory.refresh_from_db()
        self.packaging.refresh_from_db()
        self.assertEqual(self.sensory.linked_production_id, self.packaging.pk)
        self.assertEqual(self.packaging.linked_production_id, self.sensory.pk)

        resp = self.client.get(f'/{self.packaging.pk}/etap2/pakowanie/')
        self.assertContains(resp, 'Sensoryka')

    def test_link_copies_shared_data_and_checklist_before(self):
        self.sensory.data_produkcji = date(2026, 6, 1)
        self.sensory.zmiany = '2 zmiany'
        self.sensory.layout = 'L1'
        self.sensory.crm_project_nr = 'CRM1'
        self.sensory.save()
        ChecklistBefore.objects.create(
            production=self.sensory, order_updated_status='tak', pwpr_status='nie',
            additional_samples_status='tak', additional_samples_count='5',
        )

        resp = self.client.post(f'/{self.sensory.pk}/etap2/powiaz-pakowanie/',
                                 {'packaging_production': self.packaging.pk})
        self.assertEqual(resp.status_code, 302)
        self.packaging.refresh_from_db()
        self.assertEqual(self.packaging.data_produkcji, date(2026, 6, 1))
        self.assertEqual(self.packaging.zmiany, '2 zmiany')
        self.assertEqual(self.packaging.layout, 'L1')
        self.assertEqual(self.packaging.crm_project_nr, 'CRM1')

        packaging_cb = self.packaging.checklist_before
        self.assertEqual(packaging_cb.order_updated_status, 'tak')
        self.assertEqual(packaging_cb.pwpr_status, 'nie')
        self.assertEqual(packaging_cb.additional_samples_count, '5')

    def test_completing_linked_packaging_redirects_back_to_sensory(self):
        # Powiąż i wejdź na checklistę pakowania tak, jak robi to przycisk
        # "Pakowanie (powiązane zlecenie)" ze strony produkcji sensorycznej -
        # z parametrem next wskazującym z powrotem na tę stronę.
        self.client.get(f'/{self.sensory.pk}/etap2/sensoryczne/')
        self.client.post(f'/{self.sensory.pk}/etap2/powiaz-pakowanie/',
                          {'packaging_production': self.packaging.pk})
        next_url = f'/{self.sensory.pk}/'
        resp = self.client.get(f'/{self.packaging.pk}/etap2/pakowanie/?next={next_url}')
        self.assertContains(resp, f'value="{next_url}"')

        packaging_items = self.packaging.checklist_after.packaging_items.all()
        resp = self.client.post(f'/{self.packaging.pk}/etap2/pakowanie/', {
            'next': next_url,
            'packaging-TOTAL_FORMS': str(packaging_items.count()),
            'packaging-INITIAL_FORMS': str(packaging_items.count()),
            **{f'packaging-{i}-id': str(pi.pk) for i, pi in enumerate(packaging_items)},
            'complete': '1',
        })
        self.assertRedirects(resp, next_url)


class LinkedProductionCorrectionSyncTests(TestCase):
    """Powiązana para sensoryka/pakowanie jest w praktyce jedną produkcją -
    korekta i zwolnienie z jednej strony muszą synchronizować status i
    checklistę drugiej."""

    def setUp(self):
        self.sd = _make_user('sduser', 'SD')
        self.client.force_login(self.sd)
        self.sensory = FirstProduction.objects.create(
            sap_zlecenie='S1', product_name='Sensory', scope='sensory', person_sd=self.sd)
        self.packaging = FirstProduction.objects.create(
            sap_zlecenie='P1', product_name='Packaging', scope='packaging',
            fert_number='F1', recipe='R1', person_sd=self.sd)

        self.client.get(f'/{self.sensory.pk}/etap2/sensoryczne/')
        self.client.post(f'/{self.sensory.pk}/etap2/powiaz-pakowanie/',
                          {'packaging_production': self.packaging.pk})

        self.sensory.refresh_from_db()
        sensory_params = self.sensory.checklist_after.sensory_params.all()
        self.client.post(f'/{self.sensory.pk}/etap2/sensoryczne/', {
            'production_date': '2026-08-10',
            'sensory-TOTAL_FORMS': str(sensory_params.count()),
            'sensory-INITIAL_FORMS': str(sensory_params.count()),
            **{f'sensory-{i}-id': str(sp.pk) for i, sp in enumerate(sensory_params)},
            'next': '1',
        })

        self.client.get(f'/{self.packaging.pk}/etap2/pakowanie/')
        self.packaging.refresh_from_db()
        packaging_items = self.packaging.checklist_after.packaging_items.all()
        self.client.post(f'/{self.packaging.pk}/etap2/pakowanie/', {
            'packaging-TOTAL_FORMS': str(packaging_items.count()),
            'packaging-INITIAL_FORMS': str(packaging_items.count()),
            **{f'packaging-{i}-id': str(pi.pk) for i, pi in enumerate(packaging_items)},
            'complete': '1',
        })
        self.sensory.refresh_from_db()
        self.packaging.refresh_from_db()

    def test_correction_from_sensory_targeting_packaging_resets_linked_side(self):
        resp = self.client.post(f'/{self.sensory.pk}/etap3/', {
            'decision': 'correction',
            'correction_comment': 'Zle opakowanie',
            'correction_return_stage': 'packaging',
            'acceptance_signature': '',
        })
        self.assertEqual(resp.status_code, 302)
        self.sensory.refresh_from_db()
        self.packaging.refresh_from_db()
        self.assertEqual(self.sensory.status, 'etap2')
        self.assertEqual(self.packaging.status, 'etap2')
        packaging_ca = self.packaging.checklist_after
        packaging_ca.refresh_from_db()
        self.assertIsNone(packaging_ca.completed_at)
        self.assertTrue(all(pi.status == '' for pi in packaging_ca.packaging_items.all()))

    def test_correction_targeting_packaging_does_not_undo_sensory_completion(self):
        # Bug zgłoszony przez użytkownika: cofnięcie do korekty (z decyzji
        # wysłanej ze strony sensorycznej) etapu pakowania nie powinno
        # zdejmować ukończenia Etapu II ze strony sensorycznej - jej dane
        # się nie zmieniały, więc nie trzeba jej zatwierdzać jeszcze raz.
        self.client.post(f'/{self.sensory.pk}/etap3/', {
            'decision': 'correction',
            'correction_comment': 'Zle opakowanie',
            'correction_return_stage': 'packaging',
            'acceptance_signature': '',
        })
        sensory_ca = self.sensory.checklist_after
        sensory_ca.refresh_from_db()
        self.assertIsNotNone(sensory_ca.completed_at)

    def test_recompleting_packaging_after_correction_reopens_release_button(self):
        self.client.post(f'/{self.sensory.pk}/etap3/', {
            'decision': 'correction',
            'correction_comment': 'Zle opakowanie',
            'correction_return_stage': 'packaging',
            'acceptance_signature': '',
        })
        self.packaging.refresh_from_db()
        packaging_items = self.packaging.checklist_after.packaging_items.all()
        self.client.post(f'/{self.packaging.pk}/etap2/pakowanie/', {
            'packaging-TOTAL_FORMS': str(packaging_items.count()),
            'packaging-INITIAL_FORMS': str(packaging_items.count()),
            **{f'packaging-{i}-id': str(pi.pk) for i, pi in enumerate(packaging_items)},
            'complete': '1',
        })
        resp = self.client.get(f'/{self.sensory.pk}/')
        self.assertTrue(resp.context['etap2_ready'])
        self.assertContains(resp, 'Akceptacja SD &amp; Zwolnienie')

    def test_release_from_sensory_also_releases_linked_packaging(self):
        resp = self.client.post(f'/{self.sensory.pk}/etap3/', {
            'decision': 'accept',
            'acceptance_signature': '',
        })
        self.assertEqual(resp.status_code, 302)
        self.sensory.refresh_from_db()
        self.packaging.refresh_from_db()
        self.assertEqual(self.sensory.status, 'zwolniona')
        self.assertEqual(self.packaging.status, 'zwolniona')
        self.assertTrue(self.packaging.checklist_after.final_acceptance)


class LinkedEtap3GatingTests(TestCase):
    """Etap III jednej strony powiązanej pary jest dostępny tylko wtedy, gdy
    checklista Etapu II OBU stron jest ukończona - to w praktyce jedna
    produkcja, więc nie można zwolnić jednej strony, gdy druga wciąż czeka."""

    def setUp(self):
        self.sd = _make_user('sduser', 'SD')
        self.client.force_login(self.sd)
        self.sensory = FirstProduction.objects.create(
            sap_zlecenie='S1', product_name='Sensory', scope='sensory', person_sd=self.sd)
        self.packaging = FirstProduction.objects.create(
            sap_zlecenie='P1', product_name='Packaging', scope='packaging',
            fert_number='F1', recipe='R1', person_sd=self.sd)

        self.client.get(f'/{self.sensory.pk}/etap2/sensoryczne/')
        self.client.post(f'/{self.sensory.pk}/etap2/powiaz-pakowanie/',
                          {'packaging_production': self.packaging.pk})

        # tylko sensoryka jest ukończona - pakowanie jeszcze nie
        self.sensory.refresh_from_db()
        sensory_params = self.sensory.checklist_after.sensory_params.all()
        self.client.post(f'/{self.sensory.pk}/etap2/sensoryczne/', {
            'production_date': '2026-08-10',
            'sensory-TOTAL_FORMS': str(sensory_params.count()),
            'sensory-INITIAL_FORMS': str(sensory_params.count()),
            **{f'sensory-{i}-id': str(sp.pk) for i, sp in enumerate(sensory_params)},
            'next': '1',
        })
        self.sensory.refresh_from_db()

    def test_etap3_blocked_while_linked_packaging_not_done(self):
        resp = self.client.get(f'/{self.sensory.pk}/')
        self.assertNotContains(resp, 'Akceptacja SD &amp; Zwolnienie')

        resp = self.client.post(f'/{self.sensory.pk}/etap3/', {
            'decision': 'accept', 'acceptance_signature': '',
        })
        self.assertRedirects(resp, f'/{self.sensory.pk}/')
        self.sensory.refresh_from_db()
        self.assertEqual(self.sensory.status, 'etap2')

    def test_etap3_available_once_both_sides_done(self):
        self.client.get(f'/{self.packaging.pk}/etap2/pakowanie/')
        self.packaging.refresh_from_db()
        packaging_items = self.packaging.checklist_after.packaging_items.all()
        self.client.post(f'/{self.packaging.pk}/etap2/pakowanie/', {
            'packaging-TOTAL_FORMS': str(packaging_items.count()),
            'packaging-INITIAL_FORMS': str(packaging_items.count()),
            **{f'packaging-{i}-id': str(pi.pk) for i, pi in enumerate(packaging_items)},
            'complete': '1',
        })
        resp = self.client.get(f'/{self.sensory.pk}/')
        self.assertContains(resp, 'Akceptacja SD &amp; Zwolnienie')

        resp = self.client.post(f'/{self.sensory.pk}/etap3/', {
            'decision': 'accept', 'acceptance_signature': '',
        })
        self.assertEqual(resp.status_code, 302)
        self.sensory.refresh_from_db()
        self.assertEqual(self.sensory.status, 'zwolniona')


class LinkedPdfDataTests(TestCase):
    """PDF Etapu II/III dla powiązanej pary sensoryka/pakowanie musi łączyć
    dane z obu produkcji - inaczej strona pakowania nie widziała parametrów
    sensorycznych (i odwrotnie)."""

    def setUp(self):
        self.sd = _make_user('sduser', 'SD')
        self.client.force_login(self.sd)
        self.sensory = FirstProduction.objects.create(
            sap_zlecenie='S1', product_name='Sensory', scope='sensory')
        self.packaging = FirstProduction.objects.create(
            sap_zlecenie='P1', product_name='Packaging', scope='packaging',
            fert_number='F1', recipe='R1')
        self.client.get(f'/{self.sensory.pk}/etap2/sensoryczne/')
        self.client.get(f'/{self.packaging.pk}/etap2/pakowanie/')
        self.client.post(f'/{self.sensory.pk}/etap2/powiaz-pakowanie/',
                          {'packaging_production': self.packaging.pk})

    def test_packaging_side_pdf_data_includes_linked_sensory_params(self):
        from .pdf_views import _linked_checklist_data
        self.packaging.refresh_from_db()
        data = _linked_checklist_data(self.packaging)
        self.assertGreater(len(data['sensory']), 0)
        self.assertGreater(len(data['packaging']), 0)

    def test_pdf_data_merges_photos_from_both_sides(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        from .pdf_views import _linked_checklist_data
        self.sensory.refresh_from_db()
        self.packaging.refresh_from_db()
        self.sensory.checklist_after.photo_1 = SimpleUploadedFile('a.jpg', b'fake-sensory-photo')
        self.sensory.checklist_after.save()
        self.packaging.checklist_after.photo_1 = SimpleUploadedFile('b.jpg', b'fake-packaging-photo')
        self.packaging.checklist_after.save()

        data = _linked_checklist_data(self.packaging)
        self.assertEqual(len(data['photo_uris']), 2)

    def test_sensory_side_pdf_data_includes_linked_packaging_items(self):
        from .pdf_views import _linked_checklist_data
        self.sensory.refresh_from_db()
        data = _linked_checklist_data(self.sensory)
        self.assertGreater(len(data['sensory']), 0)
        self.assertGreater(len(data['packaging']), 0)


class TeamPhotoTests(TestCase):
    """Cały zespół podpisuje się odręcznie (canvas) na Etapie II - poza
    Sprzedażą Lubeck, która dokumentuje obecność zdjęciem (dodawanym w
    Etapie II, pokazywanym w PDF i dołączanym do maili procesowych)."""

    def setUp(self):
        self.sd = _make_user('sduser', 'SD')
        self.client.force_login(self.sd)
        self.rd = _make_user('rduser', 'RD')
        self.sl = _make_user('sluser', 'SL')
        self.prod = FirstProduction.objects.create(
            sap_zlecenie='1', product_name='X', scope='full', person_rd=self.rd, person_sl=self.sl)

    def test_sensory_form_has_multipart_encoding(self):
        # Pole zdjęcia Sprzedaży Lubeck jest plikiem - bez
        # enctype="multipart/form-data" przeglądarka po cichu nie wysyła go
        # w ogóle (patrz analogiczny bug naprawiony wcześniej w formularzu
        # akceptacji Etapu III).
        resp = self.client.get(f'/{self.prod.pk}/etap2/sensoryczne/')
        self.assertContains(resp, 'enctype="multipart/form-data"')

    def test_sensory_page_shows_canvas_signature_for_regular_department(self):
        resp = self.client.get(f'/{self.prod.pk}/etap2/sensoryczne/')
        self.assertContains(resp, 'team-sig-canvas')
        self.assertContains(resp, 'R&amp;D')

    def test_uploading_sl_photo_saves_it(self):
        self.client.get(f'/{self.prod.pk}/etap2/sensoryczne/')
        self.prod.refresh_from_db()
        sensory_params = self.prod.checklist_after.sensory_params.all()
        photo = SimpleUploadedFile('sl.png', _1PX_PNG, content_type='image/png')
        self.client.post(f'/{self.prod.pk}/etap2/sensoryczne/', {
            'production_date': '2026-08-10',
            'sensory-TOTAL_FORMS': str(sensory_params.count()),
            'sensory-INITIAL_FORMS': str(sensory_params.count()),
            **{f'sensory-{i}-id': str(sp.pk) for i, sp in enumerate(sensory_params)},
            'photo_sl': photo,
            'save': '1',
        })
        ca = self.prod.checklist_after
        ca.refresh_from_db()
        self.assertTrue(ca.photo_sl)

    def test_uploading_rd_signature_saves_it_not_as_photo(self):
        self.client.get(f'/{self.prod.pk}/etap2/sensoryczne/')
        self.prod.refresh_from_db()
        sensory_params = self.prod.checklist_after.sensory_params.all()
        self.client.post(f'/{self.prod.pk}/etap2/sensoryczne/', {
            'production_date': '2026-08-10',
            'sensory-TOTAL_FORMS': str(sensory_params.count()),
            'sensory-INITIAL_FORMS': str(sensory_params.count()),
            **{f'sensory-{i}-id': str(sp.pk) for i, sp in enumerate(sensory_params)},
            'sig_rd': 'data:image/png;base64,fakesignature',
            'save': '1',
        })
        ca = self.prod.checklist_after
        ca.refresh_from_db()
        self.assertEqual(ca.sig_rd, 'data:image/png;base64,fakesignature')
        self.assertFalse(hasattr(ca, 'photo_rd'))

    def test_pdf_data_merges_signatures_and_sl_photo_from_both_sides(self):
        sensory = FirstProduction.objects.create(
            sap_zlecenie='S1', product_name='Sensory', scope='sensory', person_rd=self.rd)
        sd2 = _make_user('sduser2', 'SD')
        packaging = FirstProduction.objects.create(
            sap_zlecenie='P1', product_name='Packaging', scope='packaging',
            fert_number='F1', recipe='R1')
        self.client.get(f'/{sensory.pk}/etap2/sensoryczne/')
        self.client.get(f'/{packaging.pk}/etap2/pakowanie/')
        self.client.post(f'/{sensory.pk}/etap2/powiaz-pakowanie/',
                          {'packaging_production': packaging.pk})
        # person_sd/person_sl ustawieni PO powiązaniu - łączenie kopiuje
        # wspólne dane zespołu ze strony sensorycznej na pakowanie
        # (_copy_shared_production_data), więc ustawienie przed powiązaniem
        # zostałoby nadpisane.
        packaging.refresh_from_db()
        packaging.person_sd = sd2
        packaging.person_sl = self.sl
        packaging.save(update_fields=['person_sd', 'person_sl'])
        sensory.refresh_from_db()

        sensory.checklist_after.sig_rd = 'data:image/png;base64,fakesig'
        sensory.checklist_after.save()
        packaging.checklist_after.sig_sd = 'data:image/png;base64,fakesig2'
        packaging.checklist_after.photo_sl = SimpleUploadedFile('sl.png', _1PX_PNG, content_type='image/png')
        packaging.checklist_after.save()

        from .pdf_views import _linked_checklist_data
        data = _linked_checklist_data(packaging)
        self.assertEqual(len(data['team_signatures']), 2)
        self.assertIsNotNone(data['sl_photo'])
        self.assertIsNotNone(data['sl_photo_attachment'])

    def test_sensory_accepted_email_attaches_sl_photo_not_signatures(self):
        self.client.get(f'/{self.prod.pk}/etap2/sensoryczne/')
        self.prod.refresh_from_db()
        sensory_params = self.prod.checklist_after.sensory_params.all()
        photo = SimpleUploadedFile('sl.png', _1PX_PNG, content_type='image/png')
        self.client.post(f'/{self.prod.pk}/etap2/sensoryczne/', {
            'production_date': '2026-08-10',
            'sensory-TOTAL_FORMS': str(sensory_params.count()),
            'sensory-INITIAL_FORMS': str(sensory_params.count()),
            **{f'sensory-{i}-id': str(sp.pk) for i, sp in enumerate(sensory_params)},
            'person_rd': str(self.rd.pk),
            'person_sl': str(self.sl.pk),
            'sig_rd': 'data:image/png;base64,fakesignature',
            'photo_sl': photo,
            'next': '1',
        })
        accepted_mail = next(m for m in mail.outbox if 'Sensoryka zaakceptowana' in m.subject)
        attachment_names = [a[0] for a in accepted_mail.attachments]
        self.assertTrue(any(name.startswith('Sprzedaz_Lubeck_') for name in attachment_names))
        self.assertEqual(len(attachment_names), 1)

    def test_notification_failure_does_not_500_checklist_is_still_saved(self):
        # Błąd przygotowania/wysyłki maila procesowego (np. chwilowy problem
        # z załącznikiem albo dostawcą poczty) nie może zablokować zapisu
        # checklisty - wcześniej _send_sensory_accepted_email() był wołany
        # bez zabezpieczenia, więc każdy wyjątek w tej fazie kończył się
        # błędem 500, mimo że dane były już poprawnie zapisane w bazie.
        self.client.get(f'/{self.prod.pk}/etap2/sensoryczne/')
        self.prod.refresh_from_db()
        sensory_params = self.prod.checklist_after.sensory_params.all()
        data = {
            'production_date': '2026-08-10',
            'sensory-TOTAL_FORMS': str(sensory_params.count()),
            'sensory-INITIAL_FORMS': str(sensory_params.count()),
            **{f'sensory-{i}-id': str(sp.pk) for i, sp in enumerate(sensory_params)},
            'sig_rd': 'data:image/png;base64,fakesignature',
            'next': '1',
        }
        with mock.patch('productions.views._send_sensory_accepted_email',
                         side_effect=RuntimeError('boom')):
            resp = self.client.post(f'/{self.prod.pk}/etap2/sensoryczne/', data)
        self.assertEqual(resp.status_code, 302)
        ca = self.prod.checklist_after
        ca.refresh_from_db()
        self.assertEqual(ca.sig_rd, 'data:image/png;base64,fakesignature')

    def test_email_log_recipient_field_is_not_length_limited(self):
        # Prawdziwa przyczyna błędu 500 przy w pełni obsadzonym zespole: przy
        # produkcji z osobą przypisaną do każdej roli + stałą pulą adresów
        # (management → adresy email) połączona lista adresatów
        # (', '.join(recipients)) łatwo przekracza 200 znaków. Na Postgresie
        # (produkcja) EmailLog.recipient jako CharField(max_length=200)
        # rzucał StringDataRightTruncation - na SQLite (testy/lokalnie) długość
        # nie jest egzekwowana, więc błąd nie było widać lokalnie. Pole musi
        # być bez limitu (TextField), inaczej regresja przejdzie testy mimo
        # błędu na produkcji.
        field = EmailLog._meta.get_field('recipient')
        self.assertIsNone(field.max_length)
        long_recipient_list = ', '.join(f'osoba{i}@example.com' for i in range(20))
        self.assertGreater(len(long_recipient_list), 200)
        log = EmailLog.objects.create(recipient=long_recipient_list, subject='X', body='Y')
        log.refresh_from_db()
        self.assertEqual(log.recipient, long_recipient_list)

    def test_sprzedaz_lubeck_department_selectable_and_recipient(self):
        # SL jest wybierana na checkliście sensoryki (przez dział SD), nie
        # przy tworzeniu produkcji.
        sl_user = _make_user('sluser2', 'SL', email='sl2@example.com')
        resp = self.client.get(f'/{self.prod.pk}/etap2/sensoryczne/')
        self.assertContains(resp, 'Sluser2 SL')

        prod = FirstProduction.objects.create(
            sap_zlecenie='2', product_name='Y', scope='full', person_sl=sl_user)
        from .views import _production_team_recipients
        self.assertIn('sl2@example.com', _production_team_recipients(prod))


class SapPrefillDedupeTests(TestCase):
    """AI-odczytane zlecenie SAP, które już jest w systemie, nie jest dodawane
    drugi raz."""

    def setUp(self):
        self.user = _make_user('rduser', 'RD')
        self.client.force_login(self.user)
        FirstProduction.objects.create(sap_zlecenie='11111111', product_name='Existing')

    def test_existing_sap_zlecenie_is_skipped(self):
        resp = self.client.post(
            '/api/prefill-sap/',
            data=json.dumps([
                {'sap_zlecenie': '11111111', 'sap_material': '', 'product_name': 'dup', 'data_produkcji': ''},
                {'sap_zlecenie': '22222222', 'sap_material': '', 'product_name': 'new', 'data_produkcji': ''},
            ]),
            content_type='application/json',
        )
        data = json.loads(resp.content)
        self.assertTrue(data['ok'])
        self.assertEqual(data['created'], 1)
        self.assertIn('11111111', data['skipped_existing'])
        self.assertEqual(FirstProduction.objects.filter(sap_zlecenie='11111111').count(), 1)
        self.assertEqual(FirstProduction.objects.filter(sap_zlecenie='22222222').count(), 1)


class ReleaseDecisionWorkflowTests(TestCase):
    """Etap III: Akceptacja / Akceptacja warunkowa / Do korekty."""

    def setUp(self):
        self.sd = _make_user('sduser', 'SD')
        self.client.force_login(self.sd)
        self.prod = FirstProduction.objects.create(
            sap_zlecenie='11111111', sap_material='2222', product_name='Test', scope='full',
            person_sd=self.sd)
        # drive through etap1 + etap2 to reach a completed checklist_after
        self.client.post(f'/{self.prod.pk}/etap1/', {'complete': '1'})
        ca_url_sensory = f'/{self.prod.pk}/etap2/sensoryczne/'
        self.client.get(ca_url_sensory)
        self.prod.refresh_from_db()
        sensory_params = self.prod.checklist_after.sensory_params.all()
        self.client.post(ca_url_sensory, {
            'production_date': '2026-08-10',
            'sensory-TOTAL_FORMS': str(sensory_params.count()),
            'sensory-INITIAL_FORMS': str(sensory_params.count()),
            **{f'sensory-{i}-id': str(sp.pk) for i, sp in enumerate(sensory_params)},
            'person_sd': str(self.sd.pk),
            'next': '1',
        })
        packaging_items = self.prod.checklist_after.packaging_items.all()
        self.client.post(f'/{self.prod.pk}/etap2/pakowanie/', {
            'packaging-TOTAL_FORMS': str(packaging_items.count()),
            'packaging-INITIAL_FORMS': str(packaging_items.count()),
            **{f'packaging-{i}-id': str(pi.pk) for i, pi in enumerate(packaging_items)},
            'person_sd': str(self.sd.pk),
            'umk_count': '42',
            'complete': '1',
        })
        self.prod.refresh_from_db()

    def test_correction_resets_checklist_and_notifies_without_releasing(self):
        resp = self.client.post(f'/{self.prod.pk}/etap3/', {
            'decision': 'correction',
            'correction_comment': 'Zla etykieta',
            'correction_return_stage': 'packaging',
            'acceptance_signature': '',
        })
        self.assertEqual(resp.status_code, 302)
        self.prod.refresh_from_db()
        self.assertEqual(self.prod.status, 'etap2')
        ca = self.prod.checklist_after
        ca.refresh_from_db()
        self.assertIsNone(ca.completed_at)
        self.assertTrue(all(pi.status == '' for pi in ca.packaging_items.all()))
        correction_log = EmailLog.objects.filter(production=self.prod, subject__icontains='korekty').last()
        self.assertIsNotNone(correction_log)
        self.assertIn('Zla etykieta', correction_log.body)

    def test_accept_releases_and_email_has_umk_and_material(self):
        # Liczba UMK (42) została ustawiona w Etapie II (pakowanie) w setUp -
        # Etap III już nie pyta o nią, ale wartość powinna przejść do maila.
        resp = self.client.post(f'/{self.prod.pk}/etap3/', {
            'decision': 'accept',
            'acceptance_signature': '',
        })
        self.assertEqual(resp.status_code, 302)
        self.prod.refresh_from_db()
        self.assertEqual(self.prod.status, 'zwolniona')
        release_log = EmailLog.objects.filter(production=self.prod, subject__icontains='Zwolniona').last()
        self.assertIsNotNone(release_log)
        self.assertIn('42', release_log.body)
        self.assertIn(self.prod.sap_material, release_log.body)

    def test_release_form_has_multipart_encoding(self):
        # Formularz ma pola do wgrywania zdjęć (photo_1..4) - bez
        # enctype="multipart/form-data" na <form> przeglądarka wysyła je jako
        # application/x-www-form-urlencoded, w którym pliki są po cichu
        # ignorowane (bez błędu!), więc zdjęcie nigdy nie trafia do serwera,
        # mimo że resztę formularza (decyzję SD) zapisuje bez problemu.
        # Test klienta Django wysyła multipart niezależnie od atrybutu
        # <form> w HTML, więc tego typu regresji nie wykryją testy samego
        # POST-a - trzeba sprawdzić samo wyrenderowane HTML.
        resp = self.client.get(f'/{self.prod.pk}/etap3/')
        self.assertContains(resp, 'enctype="multipart/form-data"')

    def test_accept_with_photo_attaches_it_to_release_email(self):
        # Zdjęcia w PDF są zmniejszone do layoutu strony - w mailu o
        # zwolnieniu mają być też jako osobne pliki w pełnej rozdzielczości.
        photo = SimpleUploadedFile('a.png', _1PX_PNG, content_type='image/png')
        resp = self.client.post(f'/{self.prod.pk}/etap3/', {
            'decision': 'accept',
            'acceptance_signature': '',
            'photo_1': photo,
        })
        self.assertEqual(resp.status_code, 302)
        release_mail = next(m for m in mail.outbox if 'Zwolniona' in m.subject)
        attachment_names = [a[0] for a in release_mail.attachments]
        self.assertTrue(any(name.startswith('Zdjecie_1_') and name.endswith('.png')
                             for name in attachment_names))

    def test_uploaded_photo_is_reachable_over_http_even_with_debug_off(self):
        # Bez własnego routingu dla MEDIA_URL, django.conf.urls.static.static()
        # jest no-opem gdy DEBUG=False (produkcja na Azure) - zdjęcie fizycznie
        # istnieje na dysku, ale strona/mail linkują do adresu, który wtedy
        # zwraca 404.
        photo = SimpleUploadedFile('a.png', _1PX_PNG, content_type='image/png')
        self.client.post(f'/{self.prod.pk}/etap3/', {
            'decision': 'accept',
            'acceptance_signature': '',
            'photo_1': photo,
        })
        ca = self.prod.checklist_after
        ca.refresh_from_db()
        with override_settings(DEBUG=False):
            resp = self.client.get(ca.photo_1.url)
        self.assertEqual(resp.status_code, 200)

    def test_released_production_shows_readonly_photo_gallery(self):
        # Zdjęcia są wpisywane wyłącznie w formularzu Etapu III (akceptacja)
        # - strona produkcji ma pokazywać wyłącznie podgląd tego, co tam
        # zapisano, bez osobnej możliwości dodania/podmiany zdjęcia gdzie
        # indziej.
        photo = SimpleUploadedFile('a.png', _1PX_PNG, content_type='image/png')
        self.client.post(f'/{self.prod.pk}/etap3/', {
            'decision': 'accept',
            'acceptance_signature': '',
            'photo_1': photo,
        })
        resp = self.client.get(f'/{self.prod.pk}/')
        self.assertContains(resp, 'Zdjęcia (1)')
        self.assertContains(resp, 'id="galeria-zdjec"')
        self.assertNotContains(resp, 'Wybierz plik')

    def test_conditional_requires_comment(self):
        resp = self.client.post(f'/{self.prod.pk}/etap3/', {
            'decision': 'conditional',
            'acceptance_signature': '',
        })
        self.assertEqual(resp.status_code, 200)
        self.assertIn('conditional_comment', resp.context['form'].errors)

    def test_no_separate_packaging_accepted_email(self):
        self.assertEqual(
            EmailLog.objects.filter(production=self.prod, subject__icontains='Pakowanie zaakceptowane').count(),
            0,
        )

    def test_etap3_form_has_no_umk_count_input(self):
        resp = self.client.get(f'/{self.prod.pk}/etap3/')
        self.assertNotIn('umk_count', resp.context['form'].fields)
        self.assertContains(resp, '42')  # wartość z Etapu II jest tylko wyświetlana

    def test_umk_count_is_pulled_from_etap1_additional_samples(self):
        prod = FirstProduction.objects.create(
            sap_zlecenie='33333333', sap_material='4444', product_name='Test2', scope='full',
            person_sd=self.sd)
        self.client.post(f'/{prod.pk}/etap1/', {
            'additional_samples_status': 'tak',
            'additional_samples_count': '7',
            'complete': '1',
        })
        self.client.get(f'/{prod.pk}/etap2/sensoryczne/')
        prod.refresh_from_db()
        self.assertEqual(prod.checklist_after.umk_count, '7')


class PackagingLineEtap1Tests(TestCase):
    """Linia pakująca jest wpisywana w Etapie I (nie w checkliście Etapu II
    sensorycznej/pakowania) i zapisywana na samej produkcji."""

    def setUp(self):
        self.sd = _make_user('sduser', 'SD')
        self.client.force_login(self.sd)
        self.prod = FirstProduction.objects.create(
            sap_zlecenie='55555555', sap_material='6666', product_name='Test3', scope='full')

    def test_etap1_saves_packaging_line_on_production(self):
        self.client.post(f'/{self.prod.pk}/etap1/', {
            'packaging_line': 'L3', 'save': '1',
        })
        self.prod.refresh_from_db()
        self.assertEqual(self.prod.packaging_line, 'L3')

    def test_etap2_sensory_form_has_no_packaging_line_input(self):
        self.client.post(f'/{self.prod.pk}/etap1/', {'packaging_line': 'L3', 'save': '1'})
        resp = self.client.get(f'/{self.prod.pk}/etap2/sensoryczne/')
        self.assertNotIn('packaging_line', resp.context['form'].fields)
        self.assertContains(resp, 'L3')  # wyświetlana, nie do edycji


class SapPrefillScopeTests(TestCase):
    """Wiersz z importu masowego może dostać zakres (scope) per wiersz;
    5-cyfrowy numer materiału domyślnie sugeruje 'tylko pakowanie'."""

    def setUp(self):
        self.user = _make_user('rduser', 'RD')
        self.client.force_login(self.user)

    def test_default_scope_heuristic(self):
        from .views import _default_scope_for_material
        self.assertEqual(_default_scope_for_material('12345'), 'packaging')
        self.assertEqual(_default_scope_for_material('1234'), 'full')
        self.assertEqual(_default_scope_for_material('123456'), 'full')
        self.assertEqual(_default_scope_for_material(''), 'full')
        self.assertEqual(_default_scope_for_material('12A45'), 'full')

    def test_prefill_respects_scope_per_row(self):
        resp = self.client.post(
            '/api/prefill-sap/',
            data=json.dumps([
                {'sap_zlecenie': '1', 'sap_material': '12345', 'product_name': 'pack', 'scope': 'packaging'},
                {'sap_zlecenie': '2', 'sap_material': '999', 'product_name': 'full', 'scope': 'full'},
            ]),
            content_type='application/json',
        )
        data = json.loads(resp.content)
        self.assertTrue(data['ok'])
        self.assertEqual(FirstProduction.objects.get(sap_zlecenie='1').scope, 'packaging')
        self.assertEqual(FirstProduction.objects.get(sap_zlecenie='2').scope, 'full')

    def test_prefill_falls_back_to_full_on_invalid_scope(self):
        resp = self.client.post(
            '/api/prefill-sap/',
            data=json.dumps([
                {'sap_zlecenie': '3', 'sap_material': '1', 'product_name': 'x', 'scope': 'bogus'},
            ]),
            content_type='application/json',
        )
        self.assertTrue(json.loads(resp.content)['ok'])
        self.assertEqual(FirstProduction.objects.get(sap_zlecenie='3').scope, 'full')


class UserFormAndBulkImportTests(TestCase):
    """Formularz użytkownika: scalone imię i nazwisko, brak telefonu, oraz
    masowy import z pliku Excel."""

    def setUp(self):
        self.admin = _make_user('admin', 'SD')
        self.admin.is_staff = True
        self.admin.save()
        self.client.force_login(self.admin)

    def test_create_user_splits_full_name(self):
        resp = self.client.post('/uzytkownicy/nowy/', {
            'full_name': 'Anna Maria Kowalska',
            'email': 'anna@example.com',
            'department': 'QA',
            'chip_number': '11111',
        })
        self.assertEqual(resp.status_code, 302, resp.context['form'].errors if resp.status_code == 200 else None)
        user = User.objects.get(email='anna@example.com')
        self.assertEqual(user.first_name, 'Anna Maria')
        self.assertEqual(user.last_name, 'Kowalska')

    def test_user_form_has_no_phone_field(self):
        resp = self.client.get('/uzytkownicy/nowy/')
        self.assertNotContains(resp, 'name="phone"')

    def test_create_user_accepts_single_word_name(self):
        resp = self.client.post('/uzytkownicy/nowy/', {
            'full_name': 'Prince',
            'email': 'prince@example.com',
            'department': 'QA',
            'chip_number': '22222',
        })
        self.assertEqual(resp.status_code, 302, resp.context['form'].errors if resp.status_code == 200 else None)
        user = User.objects.get(email='prince@example.com')
        self.assertEqual(user.first_name, 'Prince')
        self.assertEqual(user.last_name, '')

    def test_create_user_without_email_derives_username_from_name(self):
        resp = self.client.post('/uzytkownicy/nowy/', {
            'full_name': 'Jan Kowalski',
            'email': '',
            'department': 'QA',
            'chip_number': '33333',
        })
        self.assertEqual(resp.status_code, 302, resp.context['form'].errors if resp.status_code == 200 else None)
        user = User.objects.get(username='jan.kowalski')
        self.assertEqual(user.email, '')
        self.assertEqual(user.first_name, 'Jan')

    def test_two_users_without_email_do_not_collide_on_uniqueness_check(self):
        for chip in ('44444', '55555'):
            resp = self.client.post('/uzytkownicy/nowy/', {
                'full_name': 'Adam Nowak', 'email': '', 'department': 'QA', 'chip_number': chip,
            })
            self.assertEqual(resp.status_code, 302, resp.context['form'].errors if resp.status_code == 200 else None)
        usernames = set(User.objects.filter(first_name='Adam', last_name='Nowak').values_list('username', flat=True))
        self.assertEqual(usernames, {'adam.nowak', 'adam.nowak1'})

    def test_edit_form_still_requires_email(self):
        resp = self.client.get('/uzytkownicy/nowy/')
        self.assertNotContains(resp, 'Email służbowy *')
        user = User.objects.create_user(username='existing', first_name='Existing', last_name='User')
        resp = self.client.get(f'/uzytkownicy/{user.pk}/edytuj/')
        self.assertContains(resp, 'Email służbowy *')

    def test_bulk_import_creates_users_and_reports_errors(self):
        import openpyxl
        from io import BytesIO
        from django.core.files.uploadedfile import SimpleUploadedFile

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(['Imię i nazwisko', 'Email', 'Dział', 'Numer chip'])
        ws.append(['Jan Kowalski', 'jan.k@example.com', 'R&D', 4821])
        ws.append(['Ewa Nowak', 'ewa.n@example.com', 'PP', '04822'])
        ws.append(['Zły Wiersz', 'zly@example.com', 'NieistniejacyDzial', '04823'])
        buf = BytesIO()
        wb.save(buf)
        buf.seek(0)

        upload = SimpleUploadedFile('users.xlsx', buf.read(),
                                    content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        resp = self.client.post('/uzytkownicy/import/', {'excel_file': upload})
        self.assertEqual(resp.status_code, 200)
        results = resp.context['results']
        self.assertEqual(len(results['created']), 2)
        self.assertEqual(len(results['errors']), 1)

        jan = User.objects.get(email='jan.k@example.com')
        self.assertEqual(jan.first_name, 'Jan')
        self.assertEqual(jan.last_name, 'Kowalski')
        self.assertEqual(jan.profile.chip_number, '04821')  # zero-padded back from the float 4821.0
        self.assertEqual(jan.profile.department, 'RD')

        ewa = User.objects.get(email='ewa.n@example.com')
        self.assertEqual(ewa.profile.department, 'PP')

    @staticmethod
    def _make_upload(rows):
        import openpyxl
        from io import BytesIO
        from django.core.files.uploadedfile import SimpleUploadedFile

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(['Imię i nazwisko', 'Email', 'Dział', 'Numer chip'])
        for row in rows:
            ws.append(row)
        buf = BytesIO()
        wb.save(buf)
        buf.seek(0)
        return SimpleUploadedFile(
            'users.xlsx', buf.read(),
            content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')

    def test_bulk_import_rerun_updates_existing_user_instead_of_erroring(self):
        upload1 = self._make_upload([['Jan Kowalski', 'jan.k@example.com', 'RD', '04821']])
        self.client.post('/uzytkownicy/import/', {'excel_file': upload1})
        jan = User.objects.get(email='jan.k@example.com')
        self.assertEqual(jan.profile.chip_number, '04821')

        upload2 = self._make_upload([['Jan Kowalski', 'jan.k@example.com', 'QA', '05555']])
        resp = self.client.post('/uzytkownicy/import/', {'excel_file': upload2})
        results = resp.context['results']
        self.assertEqual(len(results['created']), 0)
        self.assertEqual(len(results['updated']), 1)
        self.assertEqual(len(results['errors']), 0)
        self.assertEqual(User.objects.filter(email='jan.k@example.com').count(), 1)
        jan.refresh_from_db()
        self.assertEqual(jan.profile.department, 'QA')
        self.assertEqual(jan.profile.chip_number, '05555')

    def test_bulk_import_update_handles_legacy_user_without_profile(self):
        # Konta z czasu przed dodaniem modelu UserProfile (albo z innego
        # powodu bez profilu) nie mogą wywalać importu błędem
        # RelatedObjectDoesNotExist przy próbie ich zaktualizowania.
        legacy = User.objects.create_user(
            username='legacy', email='legacy@example.com',
            first_name='Legacy', last_name='User')
        UserProfile.objects.filter(user=legacy).delete()

        upload = self._make_upload([['Legacy User', 'legacy@example.com', 'QA', '09999']])
        resp = self.client.post('/uzytkownicy/import/', {'excel_file': upload})
        results = resp.context['results']
        self.assertEqual(len(results['updated']), 1)
        self.assertEqual(len(results['errors']), 0)
        legacy.refresh_from_db()
        self.assertEqual(legacy.profile.department, 'QA')
        self.assertEqual(legacy.profile.chip_number, '09999')

    def test_bulk_import_allows_blank_chip_number(self):
        upload = self._make_upload([['Ola Bez Chipu', 'ola@example.com', 'QA', '']])
        resp = self.client.post('/uzytkownicy/import/', {'excel_file': upload})
        results = resp.context['results']
        self.assertEqual(len(results['created']), 1)
        self.assertEqual(len(results['errors']), 0)
        ola = User.objects.get(email='ola@example.com')
        self.assertEqual(ola.profile.chip_number, '')
        self.assertFalse(ola.has_usable_password())


class AdminUserPasswordLinkRemovedTests(TestCase):
    """Logowanie jest wyłącznie po numerze chip - w panelu /admin/ nie
    powinno być już pola/ikony do (rozjeżdżającej się z chipem) zmiany
    hasła Django."""

    def setUp(self):
        self.superuser = User.objects.create_superuser('root', 'root@example.com', 'x')
        self.client.force_login(self.superuser)

    def test_change_form_has_no_password_field_or_link(self):
        target = _make_user('someone', 'QA')
        resp = self.client.get(f'/admin/auth/user/{target.pk}/change/')
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, 'id_password')
        self.assertNotContains(resp, 'password/')


class UserDeleteTests(TestCase):
    """Usuwanie kont: tylko admin, nigdy własne konto."""

    def setUp(self):
        self.admin = _make_user('admin', 'SD')
        self.admin.is_staff = True
        self.admin.save()
        self.other = _make_user('other', 'QA')

    def test_staff_can_delete_other_user(self):
        self.client.force_login(self.admin)
        resp = self.client.post(f'/uzytkownicy/{self.other.pk}/usun/')
        self.assertRedirects(resp, '/uzytkownicy/')
        self.assertFalse(User.objects.filter(pk=self.other.pk).exists())

    def test_staff_cannot_delete_own_account(self):
        self.client.force_login(self.admin)
        resp = self.client.post(f'/uzytkownicy/{self.admin.pk}/usun/')
        self.assertRedirects(resp, '/uzytkownicy/')
        self.assertTrue(User.objects.filter(pk=self.admin.pk).exists())

    def test_non_staff_cannot_delete_user(self):
        # /uzytkownicy/ samo w sobie jest teraz dostępne tylko dla roli Admin
        # (is_staff) - więc dla użytkownika spoza tej roli przekierowanie z
        # usuwania konta samo dalej przekierowuje (do dashboardu), stąd
        # target_status_code=302 zamiast domyślnego 200.
        self.client.force_login(self.other)
        resp = self.client.post(f'/uzytkownicy/{self.admin.pk}/usun/')
        self.assertRedirects(resp, '/uzytkownicy/', target_status_code=302)
        self.assertTrue(User.objects.filter(pk=self.admin.pk).exists())

    def test_user_list_has_no_login_column(self):
        self.client.force_login(self.admin)
        resp = self.client.get('/uzytkownicy/')
        self.assertNotContains(resp, '<th>Login</th>')

    def test_non_admin_cannot_reach_user_panel_views(self):
        # "Osoby"/Użytkownicy jest dostępne tylko dla roli Admin (is_staff) -
        # dla każdego innego użytkownika każdy widok panelu ma przekierować,
        # a nie zwrócić 200.
        self.client.force_login(self.other)
        for path in (
            '/uzytkownicy/',
            '/uzytkownicy/nowy/',
            f'/uzytkownicy/{self.admin.pk}/edytuj/',
            f'/uzytkownicy/{self.admin.pk}/chip/',
            '/uzytkownicy/import/',
        ):
            resp = self.client.get(path)
            self.assertEqual(resp.status_code, 302, f'{path} should redirect for non-admin')

    def test_admin_link_hidden_from_nav_for_non_admin(self):
        self.client.force_login(self.other)
        resp = self.client.get('/')
        self.assertNotContains(resp, 'Użytkownicy')

    def test_admin_link_shown_in_nav_for_admin(self):
        self.client.force_login(self.admin)
        resp = self.client.get('/')
        self.assertContains(resp, 'Użytkownicy')


class ChipLoginAuthCodeTests(TestCase):
    """Logowanie chip + kod autoryzujący ustawiany przez użytkownika przy
    pierwszym logowaniu (i ponownie po zresetowaniu przez admina)."""

    def setUp(self):
        cache.clear()
        self.user = _make_user('chipuser', 'QA')
        self.user.profile.chip_number = '12345'
        self.user.profile.save()

    def _submit_chip(self, chip_number='12345'):
        return self.client.post('/login/', {'chip_number': chip_number}, follow=True)

    def test_first_login_prompts_to_set_code(self):
        resp = self._submit_chip()
        self.assertContains(resp, 'Nowy kod autoryzujący')
        self.assertFalse(resp.wsgi_request.user.is_authenticated)

    def test_wrong_chip_number_does_not_start_pending_flow(self):
        resp = self._submit_chip(chip_number='99999')
        self.assertContains(resp, 'Nieprawidłowy numer chip')
        self.assertFalse(resp.wsgi_request.user.is_authenticated)

    def test_set_code_mismatch_does_not_log_in(self):
        self._submit_chip()
        resp = self.client.post('/login/', {
            'new_code': 'ABC123', 'new_code_confirm': 'XYZ999',
        }, follow=True)
        self.assertContains(resp, 'nie mają')
        self.assertFalse(resp.wsgi_request.user.is_authenticated)
        self.user.profile.refresh_from_db()
        self.assertFalse(self.user.profile.has_auth_code)

    def test_set_code_success_logs_in_and_hashes_code(self):
        self._submit_chip()
        resp = self.client.post('/login/', {
            'new_code': 'ab12cd', 'new_code_confirm': 'ab12cd',
        }, follow=True)
        self.assertTrue(resp.wsgi_request.user.is_authenticated)
        self.assertEqual(resp.wsgi_request.user.pk, self.user.pk)
        self.user.profile.refresh_from_db()
        self.assertTrue(self.user.profile.has_auth_code)
        self.assertNotIn('AB12CD', self.user.profile.auth_code_hash)

    def _set_initial_code(self, code='ab12cd'):
        self._submit_chip()
        self.client.post('/login/', {'new_code': code, 'new_code_confirm': code})
        self.client.logout()
        cache.clear()

    def test_second_login_asks_for_existing_code(self):
        self._set_initial_code()
        resp = self._submit_chip()
        self.assertContains(resp, 'Kod autoryzujący')
        self.assertNotContains(resp, 'Nowy kod autoryzujący')
        self.assertFalse(resp.wsgi_request.user.is_authenticated)

    def test_verify_correct_code_logs_in(self):
        self._set_initial_code(code='ab12cd')
        self._submit_chip()
        resp = self.client.post('/login/', {'auth_code': 'AB12CD'}, follow=True)
        self.assertTrue(resp.wsgi_request.user.is_authenticated)

    def test_verify_wrong_code_fails_and_locks_out_after_repeated_attempts(self):
        self._set_initial_code(code='ab12cd')
        self._submit_chip()
        for _ in range(5):
            resp = self.client.post('/login/', {'auth_code': 'wrongg'}, follow=True)
        self.assertFalse(resp.wsgi_request.user.is_authenticated)
        self.assertContains(resp, 'Zbyt wiele nieudanych prób')

    def test_cancel_returns_to_chip_stage(self):
        self._submit_chip()
        resp = self.client.get('/login/?cancel=1', follow=True)
        self.assertContains(resp, 'Numer chip')
        self.assertNotContains(resp, 'Nowy kod autoryzujący')

    def test_admin_reset_forces_code_setup_again(self):
        self._set_initial_code()
        admin = _make_user('chipadmin', 'SD')
        admin.is_staff = True
        admin.save()
        self.client.force_login(admin)
        resp = self.client.post(f'/uzytkownicy/{self.user.pk}/reset-kod/', follow=True)
        self.assertContains(resp, 'zresetowany')
        self.user.profile.refresh_from_db()
        self.assertFalse(self.user.profile.has_auth_code)

        self.client.logout()
        cache.clear()
        resp = self._submit_chip()
        self.assertContains(resp, 'Nowy kod autoryzujący')

    def test_non_staff_cannot_reset_auth_code(self):
        self._set_initial_code()
        other = _make_user('chipother', 'QA')
        self.client.force_login(other)
        self.client.post(f'/uzytkownicy/{self.user.pk}/reset-kod/')
        self.user.profile.refresh_from_db()
        self.assertTrue(self.user.profile.has_auth_code)


class ProductionEditLinkingTests(TestCase):
    """Powiązanie z pakowaniem jest też dostępne w formularzu edycji
    produkcji, a zapis wraca na tę samą stronę (parametr 'next')."""

    def setUp(self):
        self.sd = _make_user('sduser', 'SD')
        self.client.force_login(self.sd)
        self.sensory = FirstProduction.objects.create(
            sap_zlecenie='S1', product_name='Sensory', scope='sensory')
        self.packaging = FirstProduction.objects.create(
            sap_zlecenie='P1', product_name='Packaging', scope='packaging',
            fert_number='F1', recipe='R1')

    def test_edit_page_has_link_form_when_unlinked(self):
        resp = self.client.get(f'/{self.sensory.pk}/edytuj/')
        self.assertIsNotNone(resp.context['link_form'])
        self.assertContains(resp, 'Dodaj pakownię')

    def test_edit_page_has_no_link_form_when_already_linked(self):
        self.sensory.linked_production = self.packaging
        self.sensory.save(update_fields=['linked_production'])
        resp = self.client.get(f'/{self.sensory.pk}/edytuj/')
        self.assertIsNone(resp.context['link_form'])

    def test_edit_page_has_no_link_form_for_full_scope(self):
        full = FirstProduction.objects.create(
            sap_zlecenie='F1', product_name='Full', scope='full')
        resp = self.client.get(f'/{full.pk}/edytuj/')
        self.assertIsNone(resp.context['link_form'])

    def test_link_from_edit_page_redirects_back_to_edit(self):
        edit_url = f'/{self.sensory.pk}/edytuj/'
        resp = self.client.post(f'/{self.sensory.pk}/etap2/powiaz-pakowanie/', {
            'packaging_production': self.packaging.pk,
            'next': edit_url,
        })
        self.assertRedirects(resp, edit_url)
        self.sensory.refresh_from_db()
        self.assertEqual(self.sensory.linked_production_id, self.packaging.pk)

    def test_unlink_from_edit_page_redirects_back_to_edit(self):
        self.sensory.linked_production = self.packaging
        self.sensory.save(update_fields=['linked_production'])
        self.packaging.linked_production = self.sensory
        self.packaging.save(update_fields=['linked_production'])
        edit_url = f'/{self.sensory.pk}/edytuj/'
        resp = self.client.post(f'/{self.sensory.pk}/etap2/odwiaz/', {'next': edit_url})
        self.assertRedirects(resp, edit_url)
        self.sensory.refresh_from_db()
        self.assertIsNone(self.sensory.linked_production)

    def test_unlink_without_next_falls_back_to_checklist(self):
        self.sensory.linked_production = self.packaging
        self.sensory.save(update_fields=['linked_production'])
        self.packaging.linked_production = self.sensory
        self.packaging.save(update_fields=['linked_production'])
        resp = self.client.post(f'/{self.sensory.pk}/etap2/odwiaz/')
        self.assertRedirects(resp, f'/{self.sensory.pk}/etap2/sensoryczne/')


class TeamPersonDropdownTests(TestCase):
    """Listy wyboru zespołu/akceptującego mają pokazywać czytelne Imię
    Nazwisko (nie login w formacie imie.nazwisko), a akceptującym może być
    też osoba z działu CE, nie tylko SD."""

    def setUp(self):
        self.sd = _make_user('sduser', 'SD')
        self.client.force_login(self.sd)
        self.ce = _make_user('cerson', 'CE', email='ce.person@example.com')
        self.ce.first_name, self.ce.last_name = 'Ce', 'Person'
        self.ce.save()

    def test_new_production_form_shows_full_names_not_usernames(self):
        resp = self.client.get('/nowa/')
        self.assertContains(resp, 'Sduser SD')
        self.assertNotContains(resp, '>sduser<')

    def test_acceptor_choices_include_ce_department(self):
        resp = self.client.get('/nowa/')
        self.assertContains(resp, '<option value="1">Ce Person</option>'.replace('1', str(self.ce.pk)))


class ChecklistBeforeDepartmentLockTests(TestCase):
    """Wiersze checklisty Etapu I są edytowalne tylko przez dział, który je
    nadzoruje (kolumna "Nadzór") - inne wiersze są zaszarzone/zablokowane."""

    def setUp(self):
        self.qa = _make_user('qauser', 'QA')
        self.sc = _make_user('scuser', 'SC')
        self.admin = _make_user('adminuser', 'SD')
        self.admin.is_staff = True
        self.admin.save()
        self.prod = FirstProduction.objects.create(
            sap_zlecenie='1', product_name='X', scope='full')

    def test_qa_sees_own_row_enabled_and_other_row_disabled(self):
        self.client.force_login(self.qa)
        resp = self.client.get(f'/{self.prod.pk}/etap1/')
        self.assertFalse(resp.context['form'].fields['bom_set_status'].disabled)
        self.assertTrue(resp.context['form'].fields['order_updated_status'].disabled)

    def test_qa_cannot_write_to_other_departments_row(self):
        self.client.force_login(self.qa)
        self.client.post(f'/{self.prod.pk}/etap1/', {
            'packaging_line': '',
            'bom_set_status': 'tak',
            'order_updated_status': 'tak',
        })
        cb = self.prod.checklist_before
        self.assertEqual(cb.bom_set_status, 'tak')
        self.assertEqual(cb.order_updated_status, '')

    def test_sc_can_write_own_row_qa_cannot_overwrite_it_later(self):
        self.client.force_login(self.sc)
        self.client.post(f'/{self.prod.pk}/etap1/', {
            'packaging_line': '',
            'order_updated_status': 'tak',
        })
        self.client.logout()
        self.client.force_login(self.qa)
        self.client.post(f'/{self.prod.pk}/etap1/', {
            'packaging_line': '',
            'order_updated_status': 'nie',
            'bom_set_status': 'tak',
        })
        cb = self.prod.checklist_before
        cb.refresh_from_db()
        self.assertEqual(cb.order_updated_status, 'tak')
        self.assertEqual(cb.bom_set_status, 'tak')

    def test_staff_can_edit_every_row(self):
        self.client.force_login(self.admin)
        resp = self.client.get(f'/{self.prod.pk}/etap1/')
        self.assertFalse(resp.context['form'].fields['order_updated_status'].disabled)
        self.assertFalse(resp.context['form'].fields['bom_set_status'].disabled)


class ItDepartmentTests(TestCase):
    """Dział "IT" istnieje tylko do oznaczenia konta w panelu użytkowników -
    nie ma być widoczny w checklistach ani listach wyboru zespołu/
    akceptującego produkcji."""

    def setUp(self):
        self.admin = _make_user('adminuser', 'SD')
        self.admin.is_staff = True
        self.admin.save()
        self.client.force_login(self.admin)

    def test_it_selectable_when_creating_user(self):
        resp = self.client.get('/uzytkownicy/nowy/')
        self.assertContains(resp, '<option value="IT">IT</option>')

    def test_it_user_can_be_created_and_edited(self):
        resp = self.client.post('/uzytkownicy/nowy/', {
            'full_name': 'It Person', 'email': 'it.person@example.com',
            'department': 'IT', 'chip_number': '55555',
        })
        self.assertEqual(resp.status_code, 302, resp.context['form'].errors if resp.status_code == 200 else None)
        user = User.objects.get(email='it.person@example.com')
        self.assertEqual(user.profile.department, 'IT')

        resp = self.client.post(f'/uzytkownicy/{user.pk}/edytuj/', {
            'full_name': 'It Person', 'email': 'it.person@example.com', 'department': 'IT',
        })
        self.assertEqual(resp.status_code, 302, resp.context['form'].errors if resp.status_code == 200 else None)

    def test_it_department_not_in_production_team_or_acceptor_choices(self):
        resp = self.client.get('/nowa/')
        self.assertNotContains(resp, '<option value="IT">IT</option>')

    def test_it_user_sees_all_checklist_before_rows_disabled(self):
        it_user = _make_user('ituser', 'IT')
        self.client.force_login(it_user)
        prod = FirstProduction.objects.create(sap_zlecenie='9', product_name='Y', scope='full')
        resp = self.client.get(f'/{prod.pk}/etap1/')
        self.assertTrue(resp.context['form'].fields['order_updated_status'].disabled)
        self.assertTrue(resp.context['form'].fields['bom_set_status'].disabled)


class ProductionFieldDeptLockTests(TestCase):
    """"Szczegółowe informacje" (poza zakresem produkcji i komentarzem) i
    "Numery" są edytowalne tylko przez dział nadzorujący (SD, wyjątek: zakres
    produkcji - SC; komentarz - każdy; Numery - RD)."""

    def setUp(self):
        self.rd = _make_user('rduser', 'RD')
        self.sd = _make_user('sduser', 'SD')
        self.sc = _make_user('scuser', 'SC')
        self.prod = FirstProduction.objects.create(
            sap_zlecenie='1', product_name='X', scope='full',
            zmiany='stare', rd_number='R1', recipe='old-recipe')

    def test_rd_cannot_change_szczegolowe_informacje_but_can_change_numery(self):
        self.client.force_login(self.rd)
        resp = self.client.post(f'/{self.prod.pk}/edytuj/', {
            'sap_zlecenie': '1', 'sap_material': '', 'product_name': 'X', 'scope': 'sensory',
            'zmiany': 'nowe', 'rd_number': 'R2', 'recipe': 'new-recipe',
        })
        self.assertEqual(resp.status_code, 302, resp.context['form'].errors if resp.status_code == 200 else None)
        self.prod.refresh_from_db()
        self.assertEqual(self.prod.scope, 'full')      # SD/SC pole - bez zmian
        self.assertEqual(self.prod.zmiany, 'stare')     # SD pole - bez zmian
        self.assertEqual(self.prod.rd_number, 'R2')      # Numery - RD może
        self.assertEqual(self.prod.recipe, 'new-recipe')

    def test_sc_can_change_scope_but_not_other_szczegolowe_fields(self):
        self.client.force_login(self.sc)
        resp = self.client.post(f'/{self.prod.pk}/edytuj/', {
            'sap_zlecenie': '1', 'sap_material': '', 'product_name': 'X', 'scope': 'sensory',
            'zmiany': 'nowe',
        })
        self.assertEqual(resp.status_code, 302, resp.context['form'].errors if resp.status_code == 200 else None)
        self.prod.refresh_from_db()
        self.assertEqual(self.prod.scope, 'sensory')
        self.assertEqual(self.prod.zmiany, 'stare')

    def test_sd_can_change_szczegolowe_informacje_but_not_numery(self):
        self.client.force_login(self.sd)
        resp = self.client.post(f'/{self.prod.pk}/edytuj/', {
            'sap_zlecenie': '1', 'sap_material': '', 'product_name': 'X', 'scope': 'full',
            'zmiany': 'nowe', 'rd_number': 'R3',
        })
        self.assertEqual(resp.status_code, 302, resp.context['form'].errors if resp.status_code == 200 else None)
        self.prod.refresh_from_db()
        self.assertEqual(self.prod.zmiany, 'nowe')
        self.assertEqual(self.prod.rd_number, 'R1')

    def test_komentarz_editable_by_everyone(self):
        self.client.force_login(self.rd)
        resp = self.client.post(f'/{self.prod.pk}/edytuj/', {
            'sap_zlecenie': '1', 'sap_material': '', 'product_name': 'X', 'scope': 'full',
            'komentarz': 'uwaga od RD',
        })
        self.assertEqual(resp.status_code, 302, resp.context['form'].errors if resp.status_code == 200 else None)
        self.prod.refresh_from_db()
        self.assertEqual(self.prod.komentarz, 'uwaga od RD')

    def test_admin_bypasses_all_locks(self):
        self.rd.is_staff = True
        self.rd.save()
        self.client.force_login(self.rd)
        resp = self.client.post(f'/{self.prod.pk}/edytuj/', {
            'sap_zlecenie': '1', 'sap_material': '', 'product_name': 'X', 'scope': 'sensory',
            'zmiany': 'nowe przez admina',
        })
        self.assertEqual(resp.status_code, 302, resp.context['form'].errors if resp.status_code == 200 else None)
        self.prod.refresh_from_db()
        self.assertEqual(self.prod.scope, 'sensory')
        self.assertEqual(self.prod.zmiany, 'nowe przez admina')

    def test_new_production_can_still_be_created_by_non_sd_department(self):
        # scope wymaga wartości (brak blank=True) - disabled=True nie może
        # blokować tworzenia nowej produkcji nawet przez dział bez dostępu
        # do tego pola (spada na domyślną wartość modelu).
        self.client.force_login(self.rd)
        resp = self.client.post('/nowa/', {
            'sap_zlecenie': '2', 'sap_material': '', 'product_name': 'Nowa', 'scope': 'sensory',
        })
        self.assertEqual(resp.status_code, 302, resp.context['form'].errors if resp.status_code == 200 else None)
        self.assertEqual(FirstProduction.objects.get(sap_zlecenie='2').scope, 'full')


class ProductionReminderTests(TestCase):
    """Przypomnienie 24h przed zaplanowaną produkcją, do całej puli mailowej +
    zespołu danej produkcji, niezależne od przypomnienia w dniu produkcji."""

    def setUp(self):
        from django.utils import timezone
        self.sd = _make_user('sduser', 'SD')
        self.tomorrow = timezone.localdate() + timezone.timedelta(days=1)
        self.prod = FirstProduction.objects.create(
            sap_zlecenie='1', product_name='X', scope='full',
            data_produkcji=self.tomorrow, person_sd=self.sd)

    def test_sends_24h_reminder_for_production_tomorrow(self):
        from .views import _send_24h_before_production_reminders
        _send_24h_before_production_reminders()
        self.prod.refresh_from_db()
        self.assertIsNotNone(self.prod.reminder_24h_sent_at)
        reminder_mail = next(m for m in mail.outbox if 'Za 24h produkcja' in m.subject)
        self.assertIn(self.sd.email, reminder_mail.to)

    def test_does_not_resend_same_day(self):
        from .views import _send_24h_before_production_reminders
        _send_24h_before_production_reminders()
        mail.outbox.clear()
        _send_24h_before_production_reminders()
        self.assertEqual(len(mail.outbox), 0)

    def test_does_not_fire_for_production_not_tomorrow(self):
        from django.utils import timezone
        from .views import _send_24h_before_production_reminders
        self.prod.data_produkcji = timezone.localdate()
        self.prod.save(update_fields=['data_produkcji'])
        _send_24h_before_production_reminders()
        self.assertEqual(len(mail.outbox), 0)

    def test_does_not_fire_for_released_production(self):
        from .views import _send_24h_before_production_reminders
        self.prod.status = 'zwolniona'
        self.prod.save(update_fields=['status'])
        _send_24h_before_production_reminders()
        self.assertEqual(len(mail.outbox), 0)


class ChecklistBeforeConfirmationStampTests(TestCase):
    """Zapisanie checklisty Etapu I przez osobę z działu nadzorującego ma
    automatycznie zaciągnąć jej imię i nazwisko do pola potwierdzenia tego
    działu (nie ręczne wpisywanie) - widoczne potem w PDF Etapu I."""

    def setUp(self):
        self.rd = _make_user('rduser', 'RD')
        self.prod = FirstProduction.objects.create(
            sap_zlecenie='1', product_name='X', scope='full')

    def test_saving_stamps_full_name_for_own_department(self):
        self.client.force_login(self.rd)
        resp = self.client.post(f'/{self.prod.pk}/etap1/', {'save': '1'})
        self.assertEqual(resp.status_code, 302, resp.context['form'].errors if resp.status_code == 200 else None)
        cb = self.prod.checklist_before
        self.assertEqual(cb.confirm_rd, self.rd.get_full_name())

    def test_confirm_field_is_not_directly_submittable(self):
        # Nikt nie może wpisać cudzego potwierdzenia ręcznie przez POST -
        # confirm_* jest wyłączone z formularza.
        self.client.force_login(self.rd)
        self.client.post(f'/{self.prod.pk}/etap1/', {
            'save': '1', 'confirm_sd': 'Podszywacz',
        })
        cb = self.prod.checklist_before
        self.assertEqual(cb.confirm_sd, '')

    def test_department_without_confirm_mapping_does_not_stamp_anything(self):
        it_user = _make_user('ituser', 'IT')
        self.client.force_login(it_user)
        self.client.post(f'/{self.prod.pk}/etap1/', {'save': '1'})
        cb = self.prod.checklist_before
        for field in ('confirm_rd', 'confirm_sd', 'confirm_sc', 'confirm_qa', 'confirm_ql', 'confirm_te', 'confirm_pp'):
            self.assertEqual(getattr(cb, field), '')


class StageRelabelTests(TestCase):
    """Etapy przesunięte o jeden w górę po dodaniu "Etap I - Dane SAP" jako
    pierwszego etapu (dawny Etap I to teraz Etap II, itd)."""

    def setUp(self):
        self.sd = _make_user('sduser', 'SD')
        self.client.force_login(self.sd)
        self.prod = FirstProduction.objects.create(
            sap_zlecenie='1', product_name='X', scope='full')

    def test_production_detail_step_indicator_has_new_labels(self):
        resp = self.client.get(f'/{self.prod.pk}/')
        self.assertContains(resp, 'Etap I - Dane SAP')
        self.assertContains(resp, 'Etap II – Checklista przed produkcją')
        self.assertContains(resp, 'Etap III – Checklista po produkcji')
        self.assertContains(resp, 'Etap IV – Zwolnienie do sprzedaży')

    def test_checklist_before_is_now_etap_ii(self):
        resp = self.client.get(f'/{self.prod.pk}/etap1/')
        self.assertContains(resp, 'Etap II – Checklista przed produkcją')

    def test_checklist_after_sensory_is_now_etap_iii(self):
        resp = self.client.get(f'/{self.prod.pk}/etap2/sensoryczne/')
        self.assertContains(resp, 'Etap III – Parametry sensoryczne')


class TeamSelectionMovedToChecklistTests(TestCase):
    """Zespół nie jest już wybierany przy edycji produkcji - jest wybierany
    bezpośrednio na checkliście sensoryki (SD, QA, R&D, PT, CE, Sprzedaż
    Lubeck) i pakowania (SD, PP, QA, QL, CE, Sprzedaż Lubeck), przez osobę z
    odpowiedniego działu (Sprzedaż Lubeck - przez SD)."""

    def setUp(self):
        self.sd = _make_user('sduser', 'SD')
        self.qa = _make_user('qauser', 'QA')
        self.rd = _make_user('rduser', 'RD')
        self.pt = _make_user('ptuser', 'TE')
        self.ce = _make_user('ceuser', 'CE')
        self.sl = _make_user('sluser', 'SL')
        self.pp = _make_user('ppuser', 'PP')
        self.ql = _make_user('qluser', 'QL')
        self.prod = FirstProduction.objects.create(
            sap_zlecenie='1', product_name='X', scope='full')

    def test_production_edit_form_has_no_team_section(self):
        self.client.force_login(self.sd)
        resp = self.client.get(f'/{self.prod.pk}/edytuj/')
        self.assertNotContains(resp, 'name="person_rd"')
        self.assertNotContains(resp, 'name="person_sl"')

    def test_sensory_checklist_shows_team_picker_with_correct_roles(self):
        self.client.force_login(self.sd)
        resp = self.client.get(f'/{self.prod.pk}/etap2/sensoryczne/')
        for field in ('person_sd', 'person_qa', 'person_rd', 'person_te', 'person_ce', 'person_sl'):
            self.assertContains(resp, f'name="{field}"')
        self.assertNotContains(resp, 'name="person_pp"')
        self.assertNotContains(resp, 'name="person_ql"')

    def test_packaging_checklist_shows_team_picker_with_correct_roles(self):
        self.client.force_login(self.sd)
        resp = self.client.get(f'/{self.prod.pk}/etap2/pakowanie/')
        for field in ('person_sd', 'person_pp', 'person_qa', 'person_ql', 'person_ce', 'person_sl'):
            self.assertContains(resp, f'name="{field}"')
        self.assertNotContains(resp, 'name="person_rd"')
        self.assertNotContains(resp, 'name="person_te"')

    def _sensory_post(self, extra):
        self.prod.refresh_from_db()
        self.client.get(f'/{self.prod.pk}/etap2/sensoryczne/')
        self.prod.refresh_from_db()
        sensory_params = self.prod.checklist_after.sensory_params.all()
        data = {
            'sensory-TOTAL_FORMS': str(sensory_params.count()),
            'sensory-INITIAL_FORMS': str(sensory_params.count()),
            **{f'sensory-{i}-id': str(sp.pk) for i, sp in enumerate(sensory_params)},
            'save': '1',
        }
        data.update(extra)
        return self.client.post(f'/{self.prod.pk}/etap2/sensoryczne/', data)

    def test_rd_user_can_pick_only_rd_role(self):
        self.client.force_login(self.rd)
        self._sensory_post({'person_rd': str(self.rd.pk), 'person_sd': str(self.sd.pk)})
        self.prod.refresh_from_db()
        self.assertEqual(self.prod.person_rd_id, self.rd.pk)
        self.assertIsNone(self.prod.person_sd_id)  # zablokowane - RD nie może wybrać SD

    def test_sd_user_can_pick_sl_role(self):
        self.client.force_login(self.sd)
        self._sensory_post({'person_sl': str(self.sl.pk)})
        self.prod.refresh_from_db()
        self.assertEqual(self.prod.person_sl_id, self.sl.pk)

    def test_qa_user_cannot_pick_sl_role(self):
        self.client.force_login(self.qa)
        self._sensory_post({'person_sl': str(self.sl.pk), 'person_qa': str(self.qa.pk)})
        self.prod.refresh_from_db()
        self.assertIsNone(self.prod.person_sl_id)  # zablokowane - tylko SD wybiera SL
        self.assertEqual(self.prod.person_qa_id, self.qa.pk)

    def test_packaging_prefills_sd_qa_ce_from_sensory(self):
        # Każda rola jest wybierana tylko przez osobę z tego działu - SD nie
        # może ustawić QA/CE za nich, więc symulujemy trzy osobne osoby
        # zapisujące checklistę sensoryczną, każda swoją rolę.
        self.client.force_login(self.sd)
        self._sensory_post({'person_sd': str(self.sd.pk)})
        self.client.force_login(self.qa)
        self._sensory_post({'person_qa': str(self.qa.pk)})
        self.client.force_login(self.ce)
        self._sensory_post({'person_ce': str(self.ce.pk)})

        self.client.force_login(self.sd)
        resp = self.client.get(f'/{self.prod.pk}/etap2/pakowanie/')
        self.assertContains(resp, f'<option value="{self.sd.pk}" selected>')
        self.assertContains(resp, f'<option value="{self.qa.pk}" selected>')
        self.assertContains(resp, f'<option value="{self.ce.pk}" selected>')

    def test_acceptor_field_locked_to_sd_and_ce(self):
        rd_user = self.rd
        self.client.force_login(rd_user)
        resp = self.client.get(f'/{self.prod.pk}/edytuj/')
        self.assertTrue(resp.context['form'].fields['acceptor'].disabled)

        self.client.force_login(self.sd)
        resp = self.client.get(f'/{self.prod.pk}/edytuj/')
        self.assertFalse(resp.context['form'].fields['acceptor'].disabled)

        self.client.force_login(self.ce)
        resp = self.client.get(f'/{self.prod.pk}/edytuj/')
        self.assertFalse(resp.context['form'].fields['acceptor'].disabled)


class SensoryLockAfterFirstCompletionTests(TestCase):
    """Sensoryka ma być uzupełniana raz i tylko raz ma być wysyłany mail -
    po zatwierdzeniu ("next") checklista jest zablokowana, chyba że korekta
    z Etapu IV wraca do etapu sensorycznego (odblokowuje dokładnie raz)."""

    def setUp(self):
        self.sd = _make_user('sduser', 'SD')
        self.client.force_login(self.sd)
        self.prod = FirstProduction.objects.create(
            sap_zlecenie='1', product_name='X', scope='full', person_sd=self.sd)
        self.client.get(f'/{self.prod.pk}/etap2/sensoryczne/')

    def _sensory_params(self):
        self.prod.refresh_from_db()
        return self.prod.checklist_after.sensory_params.all()

    def _submit_next(self):
        sensory_params = self._sensory_params()
        return self.client.post(f'/{self.prod.pk}/etap2/sensoryczne/', {
            'production_date': '2026-08-10',
            'sensory-TOTAL_FORMS': str(sensory_params.count()),
            'sensory-INITIAL_FORMS': str(sensory_params.count()),
            **{f'sensory-{i}-id': str(sp.pk) for i, sp in enumerate(sensory_params)},
            'person_sd': str(self.sd.pk),
            'next': '1',
        })

    def test_first_submission_sets_lock_and_sends_one_email(self):
        self._submit_next()
        ca = self.prod.checklist_after
        ca.refresh_from_db()
        self.assertIsNotNone(ca.sensory_completed_at)
        sent = [m for m in mail.outbox if 'Sensoryka zaakceptowana' in m.subject]
        self.assertEqual(len(sent), 1)

    def test_second_submission_is_blocked_and_no_extra_email(self):
        self._submit_next()
        mail.outbox.clear()
        resp = self._submit_next()
        self.assertRedirects(resp, f'/{self.prod.pk}/etap2/sensoryczne/')
        sent = [m for m in mail.outbox if 'Sensoryka zaakceptowana' in m.subject]
        self.assertEqual(len(sent), 0)

    def test_locked_page_renders_disabled_fields(self):
        self._submit_next()
        resp = self.client.get(f'/{self.prod.pk}/etap2/sensoryczne/')
        self.assertContains(resp, 'zablokowana')
        self.assertTrue(resp.context['form'].fields['uwagi'].disabled)
        self.assertTrue(resp.context['sensory_fs'].forms[0].fields['status'].disabled)
        self.assertNotContains(resp, 'Zatwierdź sensorykę')

    def test_correction_to_sensory_unlocks_exactly_once(self):
        self._submit_next()
        # przejdź przez pakowanie do etapu IV
        self.prod.refresh_from_db()
        packaging_items = self.prod.checklist_after.packaging_items.all()
        self.client.post(f'/{self.prod.pk}/etap2/pakowanie/', {
            'packaging-TOTAL_FORMS': str(packaging_items.count()),
            'packaging-INITIAL_FORMS': str(packaging_items.count()),
            **{f'packaging-{i}-id': str(pi.pk) for i, pi in enumerate(packaging_items)},
            'complete': '1',
        })
        self.client.post(f'/{self.prod.pk}/etap3/', {
            'decision': 'correction',
            'correction_comment': 'Popraw sensorykę',
            'correction_return_stage': 'sensory',
            'acceptance_signature': '',
        })
        ca = self.prod.checklist_after
        ca.refresh_from_db()
        self.assertIsNone(ca.sensory_completed_at)

        # odblokowane raz - da się uzupełnić i zatwierdzić ponownie
        resp = self.client.get(f'/{self.prod.pk}/etap2/sensoryczne/')
        self.assertNotContains(resp, 'zablokowana')
        self._submit_next()
        ca.refresh_from_db()
        self.assertIsNotNone(ca.sensory_completed_at)

        # a teraz zablokowane znowu
        resp = self.client.get(f'/{self.prod.pk}/etap2/sensoryczne/')
        self.assertContains(resp, 'zablokowana')


class BulkEmailToggleTests(TestCase):
    """Globalny przełącznik (Zarządzanie → Adresy email) wyłącza wysyłkę
    maili do grupy mailowej/zespołu - do bezpiecznego testowania aplikacji
    bez zalewania prawdziwych adresów. Nie dotyczy testowego maila."""

    def setUp(self):
        self.sd = _make_user('sduser', 'SD')
        self.sd.is_staff = True
        self.sd.save()
        self.client.force_login(self.sd)
        self.prod = FirstProduction.objects.create(
            sap_zlecenie='1', product_name='X', scope='full', person_sd=self.sd)

    def test_enabled_by_default(self):
        self.assertTrue(EmailSettings.get_solo().bulk_emails_enabled)

    def test_toggle_view_flips_setting(self):
        resp = self.client.post('/ustawienia/maile/przelacz/')
        self.assertRedirects(resp, '/ustawienia/maile/')
        self.assertFalse(EmailSettings.get_solo().bulk_emails_enabled)
        self.client.post('/ustawienia/maile/przelacz/')
        self.assertTrue(EmailSettings.get_solo().bulk_emails_enabled)

    def test_disabled_blocks_actual_send_but_logs_as_skipped(self):
        from .views import _send_and_log
        settings_row = EmailSettings.get_solo()
        settings_row.bulk_emails_enabled = False
        settings_row.save()

        result = _send_and_log(self.prod, 'Temat', 'Treść', ['a@example.com'])
        self.assertTrue(result)
        self.assertEqual(len(mail.outbox), 0)
        log = EmailLog.objects.filter(production=self.prod).last()
        self.assertTrue(log.skipped)
        self.assertTrue(log.success)

    def test_enabled_sends_normally(self):
        from .views import _send_and_log
        result = _send_and_log(self.prod, 'Temat', 'Treść', ['a@example.com'])
        self.assertTrue(result)
        self.assertEqual(len(mail.outbox), 1)
        log = EmailLog.objects.filter(production=self.prod).last()
        self.assertFalse(log.skipped)

    def test_disabled_blocks_sensory_accepted_email(self):
        settings_row = EmailSettings.get_solo()
        settings_row.bulk_emails_enabled = False
        settings_row.save()
        self.client.get(f'/{self.prod.pk}/etap2/sensoryczne/')
        self.prod.refresh_from_db()
        sensory_params = self.prod.checklist_after.sensory_params.all()
        self.client.post(f'/{self.prod.pk}/etap2/sensoryczne/', {
            'production_date': '2026-08-10',
            'sensory-TOTAL_FORMS': str(sensory_params.count()),
            'sensory-INITIAL_FORMS': str(sensory_params.count()),
            **{f'sensory-{i}-id': str(sp.pk) for i, sp in enumerate(sensory_params)},
            'person_sd': str(self.sd.pk),
            'next': '1',
        })
        self.assertEqual(len(mail.outbox), 0)
        log = EmailLog.objects.filter(subject__icontains='Sensoryka zaakceptowana').last()
        self.assertIsNotNone(log)
        self.assertTrue(log.skipped)

    def test_disabled_does_not_block_test_email_button(self):
        settings_row = EmailSettings.get_solo()
        settings_row.bulk_emails_enabled = False
        settings_row.save()
        resp = self.client.post('/ustawienia/maile/test/', {'test_email': 'ktos@example.com'})
        self.assertRedirects(resp, '/ustawienia/maile/')
        self.assertEqual(len(mail.outbox), 1)


class LabSamplesDeliveredDefaultTests(TestCase):
    """Pole "Czy dostarczono próbki do laboratorium?" nie ma pokazywać pustej
    opcji ("- Select an option -") - tylko Tak/Nie, domyślnie zaznaczone Nie."""

    def setUp(self):
        self.sd = _make_user('sduser', 'SD')
        self.client.force_login(self.sd)
        self.prod = FirstProduction.objects.create(
            sap_zlecenie='1', product_name='X', scope='full', person_sd=self.sd)

    def test_new_checklist_defaults_to_nie(self):
        self.client.get(f'/{self.prod.pk}/etap2/sensoryczne/')
        self.prod.refresh_from_db()
        self.assertEqual(self.prod.checklist_after.lab_samples_delivered, 'nie')

    def test_radio_has_no_blank_option(self):
        resp = self.client.get(f'/{self.prod.pk}/etap2/sensoryczne/')
        self.assertNotContains(resp, 'Select an option')
        self.assertContains(resp, 'value="tak"')
        self.assertContains(resp, 'value="nie"')

    def test_nie_is_preselected_by_default(self):
        resp = self.client.get(f'/{self.prod.pk}/etap2/sensoryczne/')
        form = resp.context['form']
        self.assertEqual(form.initial.get('lab_samples_delivered') or form['lab_samples_delivered'].value(), 'nie')


class ZarzadzanieAdminOnlyTests(TestCase):
    """Cała sekcja "Zarządzanie" (Użytkownicy, Adresy email) jest dostępna
    tylko dla roli Admin (is_staff)."""

    def setUp(self):
        self.admin = _make_user('adminuser', 'SD')
        self.admin.is_staff = True
        self.admin.save()
        self.other = _make_user('other', 'QA')

    def test_non_admin_cannot_reach_notification_email_views(self):
        self.client.force_login(self.other)
        for path in ('/ustawienia/maile/', '/ustawienia/maile/przelacz/', '/ustawienia/maile/test/'):
            resp = self.client.post(path) if path != '/ustawienia/maile/' else self.client.get(path)
            self.assertEqual(resp.status_code, 302, f'{path} should redirect for non-admin')

    def test_non_admin_does_not_see_zarzadzanie_nav(self):
        self.client.force_login(self.other)
        resp = self.client.get('/')
        self.assertNotContains(resp, 'Adresy email')
        self.assertNotContains(resp, 'Admin panel')

    def test_admin_sees_zarzadzanie_nav(self):
        self.client.force_login(self.admin)
        resp = self.client.get('/')
        self.assertContains(resp, 'Adresy email')
        self.assertContains(resp, 'Admin panel')

    def test_admin_can_reach_notification_email_list(self):
        self.client.force_login(self.admin)
        resp = self.client.get('/ustawienia/maile/')
        self.assertEqual(resp.status_code, 200)


class ReleaseProductionRestrictedToSdCeTests(TestCase):
    """Etap IV (Akceptacja SD i zwolnienie do sprzedaży) - tylko dla działów
    SD i CE, żeby nikt poza grupą zwalniającą nie mógł tam wejść/zatwierdzić."""

    def setUp(self):
        self.sd = _make_user('sduser', 'SD')
        self.ce = _make_user('ceuser', 'CE')
        self.rd = _make_user('rduser', 'RD')
        self.prod = FirstProduction.objects.create(
            sap_zlecenie='1', product_name='X', scope='full', person_sd=self.sd)
        self.client.force_login(self.sd)
        self.client.post(f'/{self.prod.pk}/etap1/', {'complete': '1'})
        self.client.get(f'/{self.prod.pk}/etap2/sensoryczne/')
        self.prod.refresh_from_db()
        sensory_params = self.prod.checklist_after.sensory_params.all()
        self.client.post(f'/{self.prod.pk}/etap2/sensoryczne/', {
            'production_date': '2026-08-10',
            'sensory-TOTAL_FORMS': str(sensory_params.count()),
            'sensory-INITIAL_FORMS': str(sensory_params.count()),
            **{f'sensory-{i}-id': str(sp.pk) for i, sp in enumerate(sensory_params)},
            'person_sd': str(self.sd.pk),
            'next': '1',
        })
        packaging_items = self.prod.checklist_after.packaging_items.all()
        self.client.post(f'/{self.prod.pk}/etap2/pakowanie/', {
            'packaging-TOTAL_FORMS': str(packaging_items.count()),
            'packaging-INITIAL_FORMS': str(packaging_items.count()),
            **{f'packaging-{i}-id': str(pi.pk) for i, pi in enumerate(packaging_items)},
            'person_sd': str(self.sd.pk),
            'complete': '1',
        })

    def test_rd_cannot_reach_release_view(self):
        self.client.force_login(self.rd)
        resp = self.client.get(f'/{self.prod.pk}/etap3/')
        self.assertRedirects(resp, f'/{self.prod.pk}/')

        resp = self.client.post(f'/{self.prod.pk}/etap3/', {
            'decision': 'accept', 'acceptance_signature': '',
        })
        self.assertRedirects(resp, f'/{self.prod.pk}/')
        self.prod.refresh_from_db()
        self.assertNotEqual(self.prod.status, 'zwolniona')

    def test_sd_can_reach_release_view(self):
        self.client.force_login(self.sd)
        resp = self.client.get(f'/{self.prod.pk}/etap3/')
        self.assertEqual(resp.status_code, 200)

    def test_ce_can_reach_release_view(self):
        self.client.force_login(self.ce)
        resp = self.client.get(f'/{self.prod.pk}/etap3/')
        self.assertEqual(resp.status_code, 200)

    def test_release_button_hidden_from_rd_on_production_detail(self):
        self.client.force_login(self.rd)
        resp = self.client.get(f'/{self.prod.pk}/')
        self.assertNotContains(resp, f'href="/{self.prod.pk}/etap3/"')

    def test_release_button_shown_to_sd_on_production_detail(self):
        self.client.force_login(self.sd)
        resp = self.client.get(f'/{self.prod.pk}/')
        self.assertContains(resp, f'href="/{self.prod.pk}/etap3/"')


class MachineSuitableNadzorLabelTests(TestCase):
    """Wiersz "Czy maszyna produkcyjna/pakująca jest przystosowana..." ma
    pokazywać PT jako dział nadzorujący (nie CE/PP) - zarówno w checkliście
    jak i w wygenerowanym PDF-ie Etapu I."""

    def setUp(self):
        self.sd = _make_user('sduser', 'SD')
        self.client.force_login(self.sd)
        self.prod = FirstProduction.objects.create(
            sap_zlecenie='1', product_name='X', scope='full')

    def test_checklist_before_shows_pt_not_ce_pp(self):
        resp = self.client.get(f'/{self.prod.pk}/etap1/')
        self.assertNotContains(resp, 'CE / PP')

    def test_pdf_etap1_shows_pt_not_ce_pp(self):
        # Odpowiedź to binarny PDF (application/pdf), nie HTML - assertContains
        # próbowałby dekodować jako UTF-8 i wywalał się na losowych bajtach.
        resp = self.client.get(f'/{self.prod.pk}/pdf/etap1/')
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn(b'CE / PP', resp.content)

    def test_pdf_etap1_nadzor_labels_have_no_explanatory_text(self):
        # Kolumna Nadzór ma pokazywać tylko skróty działów ("R&D / QL",
        # "R&D / QA"), bez dopisków tłumaczących kiedy dany dział nadzoruje.
        resp = self.client.get(f'/{self.prod.pk}/pdf/etap1/')
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn('w przypadku przenoszenia między zakładami'.encode(), resp.content)
        self.assertNotIn('w przypadku mieszanek'.encode(), resp.content)

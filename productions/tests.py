import base64
import json
from datetime import date
from unittest import mock

from django.contrib.auth.models import User
from django.core import mail
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings

from .models import ChecklistBefore, EmailLog, FirstProduction, UserProfile

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

    def test_sprzedaz_lubeck_department_selectable_and_recipient(self):
        sl_user = _make_user('sluser2', 'SL', email='sl2@example.com')
        resp = self.client.get('/nowa/')
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
            'next': '1',
        })
        packaging_items = self.prod.checklist_after.packaging_items.all()
        self.client.post(f'/{self.prod.pk}/etap2/pakowanie/', {
            'packaging-TOTAL_FORMS': str(packaging_items.count()),
            'packaging-INITIAL_FORMS': str(packaging_items.count()),
            **{f'packaging-{i}-id': str(pi.pk) for i, pi in enumerate(packaging_items)},
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
        self.client.force_login(self.other)
        resp = self.client.post(f'/uzytkownicy/{self.admin.pk}/usun/')
        self.assertRedirects(resp, '/uzytkownicy/')
        self.assertTrue(User.objects.filter(pk=self.admin.pk).exists())

    def test_user_list_has_no_login_column(self):
        self.client.force_login(self.admin)
        resp = self.client.get('/uzytkownicy/')
        self.assertNotContains(resp, '<th>Login</th>')


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

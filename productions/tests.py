import json

from django.contrib.auth.models import User
from django.test import TestCase

from .models import EmailLog, FirstProduction


def _make_user(username, dept, email=None):
    user = User.objects.create_user(
        username=username, password='x', first_name=username.title(), last_name=dept,
        email=email or f'{username}@example.com',
    )
    user.profile.department = dept
    user.profile.save()
    return user


class FirstProductionFormRequiredFieldsTests(TestCase):
    """Pola podstawowe (poza komentarzem) są wymagane wyłącznie dla osób z RD."""

    def setUp(self):
        self.rd = _make_user('rduser', 'RD')
        self.sd = _make_user('sduser', 'SD')

    def test_rd_must_fill_detail_and_number_fields(self):
        self.client.force_login(self.rd)
        resp = self.client.post('/nowa/', {
            'sap_zlecenie': '1', 'sap_material': '2', 'product_name': 'X', 'scope': 'full',
        })
        self.assertEqual(resp.status_code, 200)
        for field in ('data_produkcji', 'zmiany', 'layout', 'typ_produkcji',
                      'rd_number', 'recipe', 'crm_project_nr'):
            self.assertIn(field, resp.context['form'].errors)

    def test_other_department_fields_stay_optional(self):
        self.client.force_login(self.sd)
        resp = self.client.post('/nowa/', {
            'sap_zlecenie': '1', 'sap_material': '2', 'product_name': 'X', 'scope': 'full',
        })
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(FirstProduction.objects.filter(sap_zlecenie='1').exists())

    def test_packaging_only_requires_fert_and_recipe(self):
        self.client.force_login(self.sd)
        resp = self.client.post('/nowa/', {
            'sap_zlecenie': '1', 'sap_material': '2', 'product_name': 'X', 'scope': 'packaging',
        })
        self.assertEqual(resp.status_code, 200)
        self.assertIn('fert_number', resp.context['form'].errors)
        self.assertIn('recipe', resp.context['form'].errors)


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
            'umk_count': '42',
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
        resp = self.client.post(f'/{self.prod.pk}/etap3/', {
            'umk_count': '50',
            'decision': 'accept',
            'acceptance_signature': '',
        })
        self.assertEqual(resp.status_code, 302)
        self.prod.refresh_from_db()
        self.assertEqual(self.prod.status, 'zwolniona')
        release_log = EmailLog.objects.filter(production=self.prod, subject__icontains='Zwolniona').last()
        self.assertIsNotNone(release_log)
        self.assertIn('50', release_log.body)
        self.assertIn(self.prod.sap_material, release_log.body)

    def test_conditional_requires_comment(self):
        resp = self.client.post(f'/{self.prod.pk}/etap3/', {
            'umk_count': '42',
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

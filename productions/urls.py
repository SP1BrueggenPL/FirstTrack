from django.urls import path
from . import views

urlpatterns = [
    # Dashboard i produkcje
    path('', views.dashboard, name='dashboard'),
    path('import-sap/', views.import_sap, name='import_sap'),
    path('nowa/', views.production_new, name='production_new'),
    path('<int:pk>/', views.production_detail, name='production_detail'),
    path('<int:pk>/edytuj/', views.production_edit, name='production_edit'),
    path('<int:pk>/etap1/', views.checklist_before, name='checklist_before'),
    path('<int:pk>/etap2/', views.checklist_after, name='checklist_after'),
    path('<int:pk>/etap2/sensoryczne/', views.checklist_after_sensory, name='checklist_after_sensory'),
    path('<int:pk>/etap2/pakowanie/', views.checklist_after_packaging, name='checklist_after_packaging'),
    path('<int:pk>/etap2/powiaz-pakowanie/', views.link_packaging_production, name='link_packaging_production'),
    path('<int:pk>/etap2/odwiaz/', views.unlink_production, name='unlink_production'),
    path('<int:pk>/wyslij-mail/', views.send_production_email, name='send_production_email'),
    path('<int:pk>/etap3/', views.release_production, name='release_production'),
    path('<int:pk>/usun/', views.production_delete, name='production_delete'),

    # PDF
    path('<int:pk>/pdf/etap1/', views.pdf_etap1, name='pdf_etap1'),
    path('<int:pk>/pdf/etap2/', views.pdf_etap2, name='pdf_etap2'),
    path('<int:pk>/pdf/etap3/', views.pdf_etap3, name='pdf_etap3'),

    # API
    path('api/prefill-sap/', views.api_prefill_sap, name='api_prefill_sap'),

    # Zarządzanie użytkownikami
    path('uzytkownicy/', views.user_list, name='user_list'),
    path('uzytkownicy/nowy/', views.user_create, name='user_create'),
    path('uzytkownicy/import/', views.user_bulk_import, name='user_bulk_import'),
    path('uzytkownicy/<int:pk>/edytuj/', views.user_edit, name='user_edit'),
    path('uzytkownicy/<int:pk>/chip/', views.user_chip, name='user_chip'),

    # Zarządzanie – stała pula adresów email (pierwsza produkcja)
    path('ustawienia/maile/', views.notification_email_list, name='notification_email_list'),
    path('ustawienia/maile/<int:pk>/usun/', views.notification_email_delete, name='notification_email_delete'),
    path('ustawienia/maile/test/', views.notification_email_test, name='notification_email_test'),
]

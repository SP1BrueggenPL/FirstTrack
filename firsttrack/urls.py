from django.contrib import admin
from django.urls import path, re_path, include
from django.conf import settings
from django.contrib.auth import views as auth_views
from django.views.static import serve
from productions.views import chip_login

urlpatterns = [
    path('admin/', admin.site.urls),
    path('login/',  chip_login, name='login'),
    path('logout/', auth_views.LogoutView.as_view(), name='logout'),
    path('', include('productions.urls')),
    # django.conf.urls.static.static() no-ops when DEBUG=False, ale ta apka
    # nie ma przed sobą nginx/serwera plików statycznych na Azure App
    # Service - bez wprost zarejestrowanego routingu przesłane zdjęcia
    # dają 404 na produkcji (DEBUG=False), mimo że fizycznie leżą w
    # MEDIA_ROOT.
    re_path(rf'^{settings.MEDIA_URL.strip("/")}/(?P<path>.*)$', serve,
            {'document_root': settings.MEDIA_ROOT}),
]

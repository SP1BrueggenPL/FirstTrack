from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static
from django.contrib.auth import views as auth_views
from productions.views import chip_login

urlpatterns = [
    path('admin/', admin.site.urls),
    path('login/',  chip_login, name='login'),
    path('logout/', auth_views.LogoutView.as_view(), name='logout'),
    path('', include('productions.urls')),
] + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)

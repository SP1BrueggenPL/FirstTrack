from django.contrib.auth.backends import BaseBackend
from django.contrib.auth.models import User

from .models import UserProfile


class ChipNumberBackend(BaseBackend):
    """Logowanie po 5-cyfrowym numerze chip (bez loginu/hasła)."""

    def authenticate(self, request, chip_number=None, **kwargs):
        if not chip_number:
            return None
        try:
            profile = UserProfile.objects.select_related('user').get(chip_number=chip_number)
        except UserProfile.DoesNotExist:
            return None
        user = profile.user
        return user if user.is_active else None

    def get_user(self, user_id):
        try:
            return User.objects.get(pk=user_id)
        except User.DoesNotExist:
            return None

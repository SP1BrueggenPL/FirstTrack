from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from django.contrib.auth.models import User
from .models import (
    FirstProduction, ChecklistBefore, ChecklistAfter,
    SensoryParam, PackagingItem, EmailLog, UserProfile,
)


class UserProfileInline(admin.StackedInline):
    model = UserProfile
    can_delete = False
    verbose_name_plural = 'Profil'


class CustomUserAdmin(UserAdmin):
    inlines = [UserProfileInline]
    list_display = ['username', 'get_full_name', 'email', 'get_department', 'is_active']

    def get_department(self, obj):
        p = getattr(obj, 'profile', None)
        return p.get_department_display() if p and p.department else '–'
    get_department.short_description = 'Dział'


admin.site.unregister(User)
admin.site.register(User, CustomUserAdmin)


class ChecklistBeforeInline(admin.StackedInline):
    model = ChecklistBefore
    extra = 0


class ChecklistAfterInline(admin.StackedInline):
    model = ChecklistAfter
    extra = 0


@admin.register(FirstProduction)
class FirstProductionAdmin(admin.ModelAdmin):
    list_display = ['sap_zlecenie', 'sap_material', 'product_name',
                    'data_produkcji', 'typ_produkcji', 'status', 'email_sent']
    list_filter  = ['status', 'typ_produkcji', 'layout', 'data_produkcji']
    search_fields = ['sap_zlecenie', 'sap_material', 'product_name']
    raw_id_fields = ['person_rd', 'person_sc', 'person_ql', 'person_qa',
                     'person_sd', 'person_sdp', 'person_pp', 'person_ce', 'acceptor']
    inlines = [ChecklistBeforeInline, ChecklistAfterInline]


@admin.register(ChecklistBefore)
class ChecklistBeforeAdmin(admin.ModelAdmin):
    list_display = ['production', 'completed_at']


@admin.register(ChecklistAfter)
class ChecklistAfterAdmin(admin.ModelAdmin):
    list_display = ['production', 'production_date', 'final_acceptance', 'completed_at']


@admin.register(EmailLog)
class EmailLogAdmin(admin.ModelAdmin):
    list_display = ['production', 'recipient', 'sent_at', 'success']
    list_filter  = ['success']

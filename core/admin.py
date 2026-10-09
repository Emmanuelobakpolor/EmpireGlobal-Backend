from django.contrib import admin

from .models import Agent, PlatformSettings


@admin.register(Agent)
class AgentAdmin(admin.ModelAdmin):
    list_display = ['code', 'name', 'phone', 'location', 'status', 'created_at']
    list_filter = ['status', 'location']
    search_fields = ['code', 'name', 'phone', 'location']
    readonly_fields = ['code']


@admin.register(PlatformSettings)
class PlatformSettingsAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        return not PlatformSettings.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False

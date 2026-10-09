from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth.forms import AdminUserCreationForm, UserChangeForm

from .models import EmailOTP, Status, User


class UserCreationForm(AdminUserCreationForm):
    class Meta:
        model = User
        fields = ('email', 'full_name', 'role')


class UserEditForm(UserChangeForm):
    class Meta:
        model = User
        fields = '__all__'


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    form = UserEditForm
    add_form = UserCreationForm
    ordering = ['-date_joined']
    list_display = ['public_id', 'email', 'full_name', 'role', 'status', 'auth_provider', 'date_joined']
    list_filter = ['role', 'status', 'auth_provider']
    search_fields = ['public_id', 'email', 'full_name', 'phone']
    readonly_fields = ['public_id', 'date_joined', 'last_login']
    fieldsets = (
        (None, {'fields': ('public_id', 'email', 'password')}),
        ('Profile', {'fields': ('full_name', 'phone', 'agent_code')}),
        ('Access', {'fields': ('role', 'status', 'auth_provider', 'email_verified', 'is_superuser')}),
        ('Dates', {'fields': ('date_joined', 'last_login')}),
    )
    add_fieldsets = (
        (None, {'classes': ('wide',), 'fields': ('email', 'full_name', 'role', 'password1', 'password2')}),
    )
    filter_horizontal = ()

    def save_model(self, request, obj, form, change):
        # Accounts created by staff skip the email OTP step
        if not change:
            obj.status = Status.ACTIVE
            obj.email_verified = True
        super().save_model(request, obj, form, change)


@admin.register(EmailOTP)
class EmailOTPAdmin(admin.ModelAdmin):
    list_display = ['user', 'created_at', 'expires_at', 'attempts', 'used_at']
    readonly_fields = ['user', 'code_hash', 'created_at', 'expires_at', 'attempts', 'used_at']

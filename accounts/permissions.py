from rest_framework.permissions import BasePermission

from .models import Role

MUST_CHANGE_PASSWORD = 'Choose a new password before continuing.'


class IsAdmin(BasePermission):
    """Admins and Super Admins who have replaced any temporary password."""

    message = 'You do not have permission to perform this action.'

    def allowed_role(self, user):
        return user.is_admin

    def has_permission(self, request, view):
        user = request.user
        if not (user and user.is_authenticated and self.allowed_role(user)):
            return False
        if user.must_change_password:
            self.message = MUST_CHANGE_PASSWORD
            return False
        return True


class IsSuperAdmin(IsAdmin):
    def allowed_role(self, user):
        return user.role == Role.SUPER_ADMIN

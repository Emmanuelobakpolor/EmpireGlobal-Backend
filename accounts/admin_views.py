"""Admin-portal endpoints for managing admin and customer accounts."""

from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.response import Response

from core import audit

from .models import Role, Status, User
from .permissions import IsAdmin, IsSuperAdmin
from .serializers import AdminAccountSerializer, CustomerAccountSerializer
from .views import CsrfAPIView, error

ADMIN_ROLES = [Role.ADMIN, Role.SUPER_ADMIN]
ROLE_LABELS = {Role.ADMIN: 'Admin', Role.SUPER_ADMIN: 'Super Admin'}


class AdminListView(CsrfAPIView):
    permission_classes = [IsSuperAdmin]

    def get(self, request):
        admins = User.objects.filter(role__in=ADMIN_ROLES).order_by('date_joined')
        return Response({'admins': AdminAccountSerializer(admins, many=True).data})

    def post(self, request):
        serializer = AdminAccountSerializer(data=request.data, context={'request': request})
        serializer.is_valid(raise_exception=True)
        admin = serializer.save()
        audit.record(request.user, 'Admin Created',
                     f'Created {ROLE_LABELS[admin.role]} account for {admin.full_name} ({admin.email}).',
                     reference=admin.public_id, status='success')
        return Response({'admin': AdminAccountSerializer(admin).data}, status=status.HTTP_201_CREATED)


class AdminDetailView(CsrfAPIView):
    permission_classes = [IsSuperAdmin]

    def get_object(self, public_id):
        return get_object_or_404(User, public_id=public_id, role__in=ADMIN_ROLES)

    def patch(self, request, public_id):
        admin = self.get_object(public_id)
        before_role, before_status = admin.role, admin.status
        serializer = AdminAccountSerializer(admin, data=request.data, partial=True, context={'request': request})
        serializer.is_valid(raise_exception=True)
        admin = serializer.save()
        if admin.status != before_status:
            activated = admin.status == Status.ACTIVE
            audit.record(request.user, 'Admin Activated' if activated else 'Admin Deactivated',
                         f"{admin.full_name}'s admin account was {'activated' if activated else 'deactivated'}.",
                         reference=admin.public_id, status='info' if activated else 'error')
        if set(request.data) - {'status'}:
            changes = []
            if admin.role != before_role:
                changes.append(f'role changed from {ROLE_LABELS[before_role]} to {ROLE_LABELS[admin.role]}')
            if request.data.get('password'):
                changes.append('password reset')
            audit.record(request.user, 'Admin Updated',
                         f"Updated {admin.full_name}'s account" + (f" ({', '.join(changes)})" if changes else '') + '.',
                         reference=admin.public_id)
        return Response({'admin': serializer.data})

    def delete(self, request, public_id):
        admin = self.get_object(public_id)
        if admin.pk == request.user.pk:
            return error('You cannot delete your own account.', 'cannot_delete_self')
        description = f'{ROLE_LABELS[admin.role]} account for {admin.full_name} ({admin.email})'
        reference = admin.public_id
        admin.delete()
        audit.record(request.user, 'Admin Deleted', f'Deleted {description}.', reference=reference, status='error')
        return Response(status=status.HTTP_204_NO_CONTENT)


class CustomerListView(CsrfAPIView):
    permission_classes = [IsAdmin]

    def get(self, request):
        customers = User.objects.filter(role=Role.CUSTOMER)
        return Response({'customers': CustomerAccountSerializer(customers, many=True).data})


class CustomerDetailView(CsrfAPIView):
    permission_classes = [IsAdmin]

    def get_object(self, public_id):
        return get_object_or_404(User, public_id=public_id, role=Role.CUSTOMER)

    def get(self, request, public_id):
        return Response({'customer': CustomerAccountSerializer(self.get_object(public_id)).data})

    def patch(self, request, public_id):
        customer = self.get_object(public_id)
        before = customer.status
        serializer = CustomerAccountSerializer(customer, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        customer = serializer.save()
        if customer.status != before:
            suspended = customer.status == Status.SUSPENDED
            audit.record(request.user, 'Customer Suspended' if suspended else 'Customer Reactivated',
                         f"{customer.full_name}'s account status changed to {customer.status}.",
                         reference=customer.public_id, agent_code=customer.agent_code,
                         status='error' if suspended else 'info')
        return Response({'customer': serializer.data})

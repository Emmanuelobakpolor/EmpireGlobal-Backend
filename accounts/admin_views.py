"""Admin-portal endpoints for managing admin and customer accounts."""

from django.db import transaction as db_transaction
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.response import Response

from core import audit

from .models import Role, Status, User, placeholder_email
from .permissions import IsAdmin, IsSuperAdmin
from .serializers import AdminAccountSerializer, CustomerAccountSerializer, StaffRegisterCustomerSerializer
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

    def post(self, request):
        """Staff register a customer, and can start their Ajo plan in the same step (`ajo`)."""
        from payments.serializers import AjoPlanSerializer, TransactionSerializer

        serializer = StaffRegisterCustomerSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        plan = None
        if request.data.get('ajo'):
            plan = AjoPlanSerializer(data=request.data['ajo'])
            if not plan.is_valid():
                return Response({'ajo': plan.errors, 'detail': first_error(plan.errors)},
                                status=status.HTTP_400_BAD_REQUEST)
        with db_transaction.atomic():
            # No password: staff-registered customers don't sign in until given access
            customer = User.objects.create_user(
                data['email'] or placeholder_email(), None, full_name=data['fullName'], phone=data['phone'],
                agent_code=data.get('agentCode', ''), role=Role.CUSTOMER, status=Status.ACTIVE,
            )
            audit.record(request.user, 'Customer Registered',
                         f'Registered {customer.full_name} ({customer.phone}'
                         f'{", " + customer.email if customer.has_email else ", no email"}).',
                         reference=customer.public_id, status='success', agent_code=customer.agent_code)
            txn = start_ajo(request.user, customer, plan.validated_data) if plan else None
        return Response({
            'customer': CustomerAccountSerializer(customer).data,
            'transaction': TransactionSerializer(txn).data if txn else None,
        }, status=status.HTTP_201_CREATED)


def first_error(errors):
    for value in errors.values():
        if isinstance(value, list) and value:
            return str(value[0])
    return 'Check the Ajo plan details.'


def start_ajo(admin, customer, data):
    from payments import ajo

    return ajo.join(customer, data['productId'], data['amount'], data['termMonths'], data['contributionFrequency'],
                    admin)


class CustomerAjoView(CsrfAPIView):
    """Staff start an Ajo plan for an existing customer."""

    permission_classes = [IsAdmin]

    def post(self, request, public_id):
        from payments.serializers import AdminTransactionSerializer, AjoPlanSerializer

        customer = get_object_or_404(User, public_id=public_id, role=Role.CUSTOMER)
        if customer.status != Status.ACTIVE:
            return Response({'detail': 'Only an active customer can start an Ajo plan.', 'code': 'not_active'},
                            status=status.HTTP_400_BAD_REQUEST)
        serializer = AjoPlanSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        with db_transaction.atomic():
            txn = start_ajo(request.user, customer, serializer.validated_data)
        return Response({'transaction': AdminTransactionSerializer(txn).data}, status=status.HTTP_201_CREATED)


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

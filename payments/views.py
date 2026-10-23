import json

from django.db import transaction as db_transaction
from django.db.models import Count
from django.http import Http404
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated, SAFE_METHODS
from rest_framework.response import Response

from accounts.models import Role
from accounts.permissions import IsAdmin, IsSuperAdmin
from accounts.views import CsrfAPIView, error
from core import audit
from core.models import AuditLog

from . import ajo, facilities, investments, services
from .files import serve_private_file
from .models import (
    FACILITY_TYPES,
    ApplicationDocument,
    BankAccount,
    Contribution,
    FacilityAssignment,
    FacilityStatus,
    InterestPayment,
    Notification,
    Repayment,
    Transaction,
)
from .notify import customer_link, notify, notify_admins, payment_review_link
from .serializers import (
    FACILITIES,
    AdminTransactionSerializer,
    ApproveSerializer,
    AssignmentSerializer,
    DisburseSerializer,
    InterestPaymentListSerializer,
    InterestRateSerializer,
    BankAccountSerializer,
    AjoPayoutSerializer,
    RecordContributionSerializer,
    CreateTransactionSerializer,
    NotificationSerializer,
    ReceiptSerializer,
    RecommendSerializer,
    ReviewSerializer,
    TransactionSerializer,
    add_months,
    assignments_dict,
)

ADMIN_QUERYSET = Transaction.objects.select_related('customer').prefetch_related(
    'slip_events', 'documents', 'repayments__events', 'interest_payments', 'contributions__events')
CUSTOMER_QUERYSET = Transaction.objects.select_related('customer').prefetch_related(
    'documents', 'repayments', 'interest_payments', 'contributions')


class IsCustomer(IsAuthenticated):
    def has_permission(self, request, view):
        return super().has_permission(request, view) and request.user.role == Role.CUSTOMER


# ---- Customer ----

class TransactionListView(CsrfAPIView):
    permission_classes = [IsCustomer]
    # JSON, or multipart when an application carries documents: `payload` (JSON) + `document.<key>` files
    parser_classes = [JSONParser, MultiPartParser, FormParser]

    def get(self, request):
        # No scheduler is needed for repayment reminders: they're also sent as customers come by
        facilities.send_reminders(customer=request.user)
        investments.send_due_notices()
        txns = CUSTOMER_QUERYSET.filter(customer=request.user)
        return Response({'transactions': TransactionSerializer(txns, many=True).data})

    def post(self, request):
        if 'payload' in request.data:
            try:
                data = json.loads(request.data['payload'])
            except (TypeError, ValueError):
                return error('Invalid request.', 'invalid_payload')
        else:
            data = request.data
        serializer = CreateTransactionSerializer(data=data, context={'files': request.FILES})
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        product = data['productId']

        now = timezone.now()
        start = timezone.localdate()
        term = data['termMonths']
        is_facility = product.type in FACILITY_TYPES
        # Loans and hire-purchase aren't paid for up front: the collection account is shown
        # for each repayment as it comes due instead. Ajo contributions are recorded by staff.
        account = None if is_facility or product.type == 'ajo' else BankAccount.for_product_type(product.type)
        with db_transaction.atomic():
            txn = Transaction.objects.create(
                customer=request.user,
                product_id=product.code,
                product_name=product.name,
                product_type=product.type,
                amount=data['amount'],
                term_months=term,
                start_date=start,
                end_date=add_months(start, term) if term else None,
                application={**data['application'], 'submittedAt': now.isoformat()} if data['application'] else None,
                next_of_kin=data.get('nextOfKin') if product.type in ('savings', 'investment') else None,
                terms_accepted_at=now if product.clauses else None,
                # What the customer is told to pay into, fixed at this moment
                paid_to=account.as_payment_details() if account else None,
                created_at=now,
                **({
                    'status': Transaction.Status.PENDING,
                    'facility_status': FacilityStatus.UNDER_REVIEW,
                    'repayment_frequency': data['repaymentFrequency'],
                    'disbursement_account': data['disbursementAccount'],
                } if is_facility else {}),
                **({
                    'interest_option': data['interestOption'],
                    'disbursement_account': data['disbursementAccount'],
                } if data['interestOption'] else {}),
                **({'repayment_frequency': data['repaymentFrequency']} if product.type == 'ajo' else {}),
            )
            if ajo.is_ajo(txn):
                ajo.start(txn, request.user)
            for spec, upload in data['documents']:
                ApplicationDocument.objects.create(
                    transaction=txn, key=spec['key'], label=spec['label'], file=upload,
                    original_name=upload.name[:255], size=upload.size,
                )
            if txn.application:
                kind = 'hire-purchase' if txn.product_type == 'hire-purchase' else 'loan'
                notify(request.user, 'Application received',
                       f"We received your {txn.product_name} application ({txn.reference}). We'll review it and "
                       "let you know the outcome. There's nothing to pay now.", Notification.Kind.APPLICATION,
                       link=customer_link(txn))
                notify_admins(f'New {kind} application',
                              f'{request.user.full_name} applied for {txn.product_name} '
                              f'(₦{float(txn.amount):,.0f}, {txn.reference}).',
                              Notification.Kind.APPLICATION, link=facilities.admin_link(txn))
        return Response({'transaction': TransactionSerializer(txn).data}, status=status.HTTP_201_CREATED)


class TransactionDetailView(CsrfAPIView):
    permission_classes = [IsCustomer]

    def get(self, request, reference):
        txn = get_object_or_404(CUSTOMER_QUERYSET, reference=reference, customer=request.user)
        return Response({'transaction': TransactionSerializer(txn).data})


class UploadReceiptView(CsrfAPIView):
    permission_classes = [IsCustomer]
    parser_classes = [MultiPartParser, FormParser]
    throttle_scope = 'uploads'

    def post(self, request, reference):
        txn = get_object_or_404(Transaction, reference=reference, customer=request.user)
        if txn.is_facility:
            return error('Loans and hire-purchase are repaid by instalment. Upload the receipt against the '
                         'instalment instead.', 'facility_repayment')
        if ajo.is_ajo(txn):
            return error('Ajo contributions are recorded by our staff.', 'ajo_recorded_by_staff')
        if txn.is_final:
            return error('This payment has already been reviewed.', 'already_decided')
        serializer = ReceiptSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        upload = serializer.validated_data['file']

        old = txn.receipt.name if txn.receipt else None
        txn.receipt = upload
        txn.receipt_name = upload.name[:255]
        txn.receipt_size = upload.size
        txn.receipt_uploaded_at = timezone.now()
        txn.status = Transaction.Status.PENDING
        txn.save()
        # A replaced slip is no longer needed
        if old and old != txn.receipt.name:
            txn.receipt.storage.delete(old)
        notify(request.user, 'Receipt received',
               f"We received your receipt for {txn.product_name} ({txn.reference}). We'll let you know once "
               'it has been verified.', Notification.Kind.RECEIPT, link=customer_link(txn))
        notify_admins('Replaced payment slip to verify' if old else 'New payment slip to verify',
                      f'{request.user.full_name} uploaded a receipt for {txn.product_name} '
                      f'(₦{float(txn.amount):,.0f}, {txn.reference}).',
                      Notification.Kind.RECEIPT, Notification.Type.WARNING, link=payment_review_link(txn))
        return Response({'transaction': TransactionSerializer(txn).data})


def _visible_transaction(request, reference):
    """The transaction if the user owns it or is an admin; otherwise a 404 that reveals nothing."""
    txn = get_object_or_404(Transaction, reference=reference)
    user = request.user
    allowed = (user.is_admin and not user.must_change_password) or txn.customer_id == user.pk
    if not allowed:
        raise Http404
    return txn


class ReceiptFileView(CsrfAPIView):
    """Streams the slip to its owner or to an admin. Files are never publicly served."""

    permission_classes = [IsAuthenticated]

    def get(self, request, reference):
        txn = _visible_transaction(request, reference)
        if not txn.receipt:
            raise Http404
        return serve_private_file(txn.receipt, txn.receipt_name)


class DocumentFileView(CsrfAPIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, reference, key):
        txn = _visible_transaction(request, reference)
        doc = get_object_or_404(ApplicationDocument, transaction=txn, key=key)
        return serve_private_file(doc.file, doc.original_name)


class NotificationListView(CsrfAPIView):
    """The signed-in person's notifications (customers and admins alike)."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        mine = Notification.objects.filter(recipient=request.user)
        return Response({
            'notifications': NotificationSerializer(mine[:100], many=True).data,
            'unreadCount': mine.filter(read=False).count(),
        })


class NotificationReadView(CsrfAPIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, pk=None):
        items = Notification.objects.filter(recipient=request.user, read=False)
        if pk is not None:
            items = items.filter(pk=pk)
        items.update(read=True)
        return Response(status=status.HTTP_204_NO_CONTENT)


# ---- Admin: payment review ----

class AdminTransactionListView(CsrfAPIView):
    permission_classes = [IsAdmin]

    def get(self, request):
        investments.send_due_notices()
        return Response({'transactions': AdminTransactionSerializer(ADMIN_QUERYSET.all(), many=True).data})


class AdminTransactionView(CsrfAPIView):
    permission_classes = [IsAdmin]

    def get_txn(self, reference):
        return get_object_or_404(ADMIN_QUERYSET, reference=reference)

    def respond(self, reference):
        return Response({'transaction': AdminTransactionSerializer(self.get_txn(reference)).data})

    def get(self, request, reference):
        return self.respond(reference)


class RecordViewView(AdminTransactionView):
    def post(self, request, reference):
        recorded = services.record_view(self.get_txn(reference), request.user)
        response = self.respond(reference)
        response.data['recorded'] = bool(recorded)
        return response


class ReviewActionView(AdminTransactionView):
    def post(self, request, reference):
        txn = self.get_txn(reference)
        try:
            self.act(request, txn)
        except services.ReviewError as exc:
            return error(exc.message, exc.code)
        return self.respond(reference)


class RecommendView(ReviewActionView):
    def act(self, request, txn):
        serializer = RecommendSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        services.recommend(txn, request.user, data['decision'] == 'approve', data['note'])


class ApproveView(ReviewActionView):
    permission_classes = [IsSuperAdmin]

    def act(self, request, txn):
        serializer = ApproveSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        services.approve_payment(txn, request.user, data['note'], data.get('interestRate'), serializer.fees())


class RejectView(ReviewActionView):
    permission_classes = [IsSuperAdmin]

    def act(self, request, txn):
        serializer = ReviewSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        services.reject_payment(txn, request.user, serializer.validated_data['note'])


class InterestRateView(AdminTransactionView):
    """A Super Admin changes the rate on an approved loan or hire-purchase, re-pricing what's unpaid."""
    permission_classes = [IsSuperAdmin]

    def post(self, request, reference):
        serializer = InterestRateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            facilities.change_interest_rate(self.get_txn(reference), request.user, data['interestRate'], data['note'])
        except facilities.FacilityError as exc:
            return facility_error(exc)
        return self.respond(reference)


class DisburseView(AdminTransactionView):
    """Any admin records that an approved loan was paid out (or the item delivered)."""

    def post(self, request, reference):
        serializer = DisburseSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            facilities.disburse(self.get_txn(reference), request.user, serializer.validated_data['reference'])
        except facilities.FacilityError as exc:
            return facility_error(exc)
        return self.respond(reference)


# ---- Investment interest ----

def investment_error(exc):
    extra = {'errors': {exc.field: [exc.message]}} if exc.field else {}
    return error(exc.message, exc.code, **extra)


class InterestPaymentListView(CsrfAPIView):
    """Every investment's interest payments, for admins to send as they come due."""
    permission_classes = [IsAdmin]

    def get(self, request):
        investments.send_due_notices()
        payments = (InterestPayment.objects.select_related('plan__customer')
                    .annotate(count=Count('plan__interest_payments')).order_by('due_date', 'pk'))
        return Response({'payments': InterestPaymentListSerializer(payments, many=True).data})


class InterestPaymentPayView(CsrfAPIView):
    """Any admin records that a due interest payment was transferred to the customer."""
    permission_classes = [IsAdmin]

    def post(self, request, pk):
        payment = get_object_or_404(InterestPayment, pk=pk)
        serializer = DisburseSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            investments.record_payment(payment, request.user, serializer.validated_data['reference'])
        except investments.InvestmentError as exc:
            return investment_error(exc)
        txn = get_object_or_404(ADMIN_QUERYSET, pk=payment.plan_id)
        return Response({'transaction': AdminTransactionSerializer(txn).data})


# ---- Repayments ----

def facility_error(exc):
    extra = {'errors': {exc.field: [exc.message]}} if exc.field else {}
    return error(exc.message, exc.code, **extra)


class RepaymentReceiptUploadView(CsrfAPIView):
    permission_classes = [IsCustomer]
    parser_classes = [MultiPartParser, FormParser]
    throttle_scope = 'uploads'

    def post(self, request, pk):
        rep = get_object_or_404(Repayment, pk=pk, facility__customer=request.user)
        serializer = ReceiptSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            facilities.upload_repayment_receipt(request.user, rep, serializer.validated_data['file'])
        except facilities.FacilityError as exc:
            return facility_error(exc)
        txn = get_object_or_404(CUSTOMER_QUERYSET, pk=rep.facility_id)
        return Response({'transaction': TransactionSerializer(txn).data})


class RepaymentReceiptFileView(CsrfAPIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        rep = get_object_or_404(Repayment.objects.select_related('facility'), pk=pk)
        _visible_transaction(request, rep.facility.reference)
        if not rep.receipt:
            raise Http404
        return serve_private_file(rep.receipt, rep.receipt_name)


class AdminRepaymentActionView(CsrfAPIView):
    permission_classes = [IsAdmin]
    action_name = ''

    def get_permissions(self):
        if self.action_name in ('approve', 'reject'):
            return [IsSuperAdmin()]
        return super().get_permissions()

    def post(self, request, pk):
        rep = get_object_or_404(Repayment, pk=pk)
        try:
            if self.action_name == 'recommend':
                serializer = RecommendSerializer(data=request.data)
                serializer.is_valid(raise_exception=True)
                facilities.recommend_repayment(rep, request.user, serializer.validated_data['decision'] == 'approve',
                                               serializer.validated_data['note'])
            else:
                serializer = ReviewSerializer(data=request.data)
                serializer.is_valid(raise_exception=True)
                action = facilities.approve_repayment if self.action_name == 'approve' else facilities.reject_repayment
                action(rep, request.user, serializer.validated_data['note'])
        except facilities.FacilityError as exc:
            return facility_error(exc)
        txn = get_object_or_404(ADMIN_QUERYSET, pk=rep.facility_id)
        return Response({'transaction': AdminTransactionSerializer(txn).data})


# ---- Ajo contributions ----

def ajo_error(exc):
    extra = {'errors': {exc.field: [exc.message]}} if exc.field else {}
    return error(exc.message, exc.code, **extra)


class AdminContributionActionView(CsrfAPIView):
    """Any admin records a payment or recommends; a Super Admin confirms or rejects."""

    permission_classes = [IsAdmin]
    action_name = ''

    def get_permissions(self):
        if self.action_name in ('approve', 'reject'):
            return [IsSuperAdmin()]
        return super().get_permissions()

    def post(self, request, pk):
        contribution = get_object_or_404(Contribution, pk=pk)
        try:
            if self.action_name == 'record':
                serializer = RecordContributionSerializer(data=request.data)
                serializer.is_valid(raise_exception=True)
                data = serializer.validated_data
                ajo.record_payment(request.user, contribution, data['method'], data['receivedOn'], data['reference'],
                                   data['note'])
            elif self.action_name == 'recommend':
                serializer = RecommendSerializer(data=request.data)
                serializer.is_valid(raise_exception=True)
                ajo.recommend(request.user, contribution, serializer.validated_data['decision'] == 'approve',
                              serializer.validated_data['note'])
            else:
                serializer = ReviewSerializer(data=request.data)
                serializer.is_valid(raise_exception=True)
                action = ajo.approve if self.action_name == 'approve' else ajo.reject
                action(request.user, contribution, serializer.validated_data['note'])
        except ajo.AjoError as exc:
            return ajo_error(exc)
        txn = get_object_or_404(ADMIN_QUERYSET, pk=contribution.plan_id)
        return Response({'transaction': AdminTransactionSerializer(txn).data})


class AjoPayoutView(AdminTransactionView):
    """Any admin arranges an Ajo plan's payout once its cycle has ended."""

    def post(self, request, reference):
        serializer = AjoPayoutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            ajo.arrange_payout(request.user, self.get_txn(reference), data['bankName'], data['accountNumber'],
                               data['accountName'], data['note'])
        except ajo.AjoError as exc:
            return ajo_error(exc)
        return self.respond(reference)


# ---- Admin: collection bank accounts ----

class SuperAdminWrites(IsAdmin):
    """Every admin can see the accounts; only a Super Admin can change where customers pay."""

    def has_permission(self, request, view):
        if request.method in SAFE_METHODS:
            return super().has_permission(request, view)
        return IsSuperAdmin().has_permission(request, view)


def bank_accounts_response(http_status=status.HTTP_200_OK):
    return Response({
        'accounts': BankAccountSerializer(BankAccount.objects.all(), many=True).data,
        'assignments': assignments_dict(),
    }, status=http_status)


class BankAccountListView(CsrfAPIView):
    permission_classes = [SuperAdminWrites]

    def get(self, request):
        return bank_accounts_response()

    def post(self, request):
        serializer = BankAccountSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        account = serializer.save(status=BankAccount.Status.ACTIVE)
        audit.record(request.user, 'Bank Account Added', f'Added collection account {account}.',
                     reference=account.account_number, status='success')
        return bank_accounts_response(status.HTTP_201_CREATED)


class BankAccountDetailView(CsrfAPIView):
    permission_classes = [SuperAdminWrites]

    def patch(self, request, code):
        account = get_object_or_404(BankAccount, code=code)
        before, before_status = str(account), account.status
        serializer = BankAccountSerializer(account, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        account = serializer.save()
        if account.status != before_status:
            activated = account.is_active
            audit.record(request.user, 'Bank Account Activated' if activated else 'Bank Account Deactivated',
                         f'{account} was {"activated" if activated else "deactivated"}.',
                         reference=account.account_number, status='info' if activated else 'error')
        if str(account) != before:
            audit.record(request.user, 'Bank Account Updated', f'Changed {before} to {account}.',
                         reference=account.account_number)
        return bank_accounts_response()

    def delete(self, request, code):
        account = get_object_or_404(BankAccount, code=code)
        description, number = str(account), account.account_number
        # Its facility mappings go too, so those facilities fall back to the default account.
        # Transactions keep their own copy of the details they were given.
        account.delete()
        audit.record(request.user, 'Bank Account Removed', f'Removed collection account {description}.',
                     reference=number, status='error')
        return bank_accounts_response()


class AssignmentView(CsrfAPIView):
    permission_classes = [IsSuperAdmin]

    def put(self, request, facility):
        if facility not in FACILITIES:
            raise Http404
        serializer = AssignmentSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        account = serializer.validated_data['accountId']
        label = request.data.get('facilityName') or facility
        if account:
            FacilityAssignment.objects.update_or_create(facility=facility, defaults={'account': account})
            details = f'{label} payments now go to {account}.'
        else:
            FacilityAssignment.objects.filter(facility=facility).delete()
            details = f'{label} now uses the default collection account.'
        audit.record(request.user, 'Bank Account Assigned', details, reference=str(label)[:100])
        return bank_accounts_response()


# ---- Admin: audit log ----

class AuditLogListView(CsrfAPIView):
    permission_classes = [IsAdmin]

    def get(self, request):
        logs = AuditLog.objects.all()[:1000]
        return Response({'auditLogs': [
            {'id': log.pk, 'date': log.created_at, 'user': log.actor_name, 'role': log.actor_role or None,
             'action': log.action, 'reference': log.reference, 'agentCode': log.agent_code or None,
             'status': log.status, 'details': log.details}
            for log in logs
        ]})


# ---- Withdrawals ----

from . import withdrawals as withdrawal_service  # noqa: E402
from .models import Withdrawal  # noqa: E402
from .serializers import (  # noqa: E402
    AdminWithdrawalSerializer,
    CreateWithdrawalSerializer,
    PayoutSerializer,
    WithdrawalQuoteSerializer,
    WithdrawalSerializer,
    money,
)
from .withdrawal_rules import quote as withdrawal_quote  # noqa: E402

WITHDRAWALS = Withdrawal.objects.select_related('customer', 'plan')


def withdrawal_error(exc):
    extra = {'errors': {exc.field: [exc.message]}} if exc.field else {}
    return error(exc.message, exc.code, **extra)


class WithdrawablePlansView(CsrfAPIView):
    """The customer's approved plans, with what can be withdrawn from each today."""

    permission_classes = [IsCustomer]

    def get(self, request):
        # Ajo payouts are arranged by staff
        plans = Transaction.objects.filter(customer=request.user, status=Transaction.Status.APPROVED).exclude(
            product_type='ajo')
        return Response({'plans': [
            {'reference': p.reference, 'productName': p.product_name, 'productType': p.product_type,
             'amount': money(p.amount), 'startDate': p.start_date, 'endDate': p.end_date,
             **withdrawal_quote(p).as_dict()}
            for p in plans
        ]})


class WithdrawalQuoteView(CsrfAPIView):
    permission_classes = [IsCustomer]

    def post(self, request):
        serializer = WithdrawalQuoteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        plan = get_object_or_404(Transaction, reference=data['planReference'], customer=request.user)
        return Response({'quote': withdrawal_quote(plan, data.get('amount')).as_dict()})


class WithdrawalListView(CsrfAPIView):
    permission_classes = [IsCustomer]
    throttle_scope = 'withdrawals'

    def get_throttles(self):
        # Only creating a withdrawal is rate limited, not listing them
        return super().get_throttles() if self.request.method == 'POST' else []

    def get(self, request):
        return Response({'withdrawals': WithdrawalSerializer(WITHDRAWALS.filter(customer=request.user), many=True).data})

    def post(self, request):
        serializer = CreateWithdrawalSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        if not request.user.check_password(data['password']):
            return error('Your password is incorrect.', 'invalid_password',
                         errors={'password': ['Your password is incorrect.']})
        try:
            wd = withdrawal_service.request_withdrawal(
                request.user, data['planReference'], data['amount'], data['bankName'], data['accountNumber'],
                data['accountName'], data['note'],
            )
        except withdrawal_service.WithdrawalError as exc:
            return withdrawal_error(exc)
        return Response({'withdrawal': WithdrawalSerializer(wd).data}, status=status.HTTP_201_CREATED)


class CancelWithdrawalView(CsrfAPIView):
    permission_classes = [IsCustomer]

    def post(self, request, reference):
        wd = get_object_or_404(Withdrawal, reference=reference, customer=request.user)
        try:
            wd = withdrawal_service.cancel(request.user, wd)
        except withdrawal_service.WithdrawalError as exc:
            return withdrawal_error(exc)
        return Response({'withdrawal': WithdrawalSerializer(wd).data})


class AdminWithdrawalListView(CsrfAPIView):
    permission_classes = [IsAdmin]

    def get(self, request):
        rows = WITHDRAWALS.prefetch_related('events')
        return Response({'withdrawals': AdminWithdrawalSerializer(rows, many=True).data})


class AdminWithdrawalView(CsrfAPIView):
    permission_classes = [IsAdmin]

    def get_wd(self, reference):
        return get_object_or_404(WITHDRAWALS.prefetch_related('events'), reference=reference)

    def respond(self, reference):
        wd = self.get_wd(reference)
        data = AdminWithdrawalSerializer(wd).data
        # The plan today, counting this withdrawal and any others in progress
        data['plan'] = {'reference': wd.plan.reference, 'amount': money(wd.plan.amount),
                        'startDate': wd.plan.start_date, 'endDate': wd.plan.end_date,
                        **withdrawal_quote(wd.plan).as_dict()}
        return Response({'withdrawal': data})

    def get(self, request, reference):
        return self.respond(reference)


class AdminWithdrawalActionView(AdminWithdrawalView):
    action_name = ''

    def get_permissions(self):
        # Final decisions are for Super Admins; recommending and marking paid are for any admin
        if self.action_name in ('approve', 'reject'):
            return [IsSuperAdmin()]
        return super().get_permissions()

    def post(self, request, reference):
        wd = self.get_wd(reference)
        try:
            if self.action_name == 'recommend':
                serializer = RecommendSerializer(data=request.data)
                serializer.is_valid(raise_exception=True)
                withdrawal_service.recommend(request.user, wd, serializer.validated_data['decision'] == 'approve',
                                             serializer.validated_data['note'])
            elif self.action_name in ('approve', 'reject'):
                serializer = ReviewSerializer(data=request.data)
                serializer.is_valid(raise_exception=True)
                action = withdrawal_service.approve if self.action_name == 'approve' else withdrawal_service.reject
                action(request.user, wd, serializer.validated_data['note'])
            elif self.action_name == 'mark-paid':
                serializer = PayoutSerializer(data=request.data)
                serializer.is_valid(raise_exception=True)
                withdrawal_service.mark_paid(request.user, wd, serializer.validated_data['payoutReference'])
        except withdrawal_service.WithdrawalError as exc:
            return withdrawal_error(exc)
        return self.respond(reference)

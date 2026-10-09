"""Creating notifications. Called from the place where each event happens."""

from accounts.models import Role, Status, User

from .models import Notification

ADMIN_ROLES = (Role.ADMIN, Role.SUPER_ADMIN)


def notify(recipient, title, message, kind, type=Notification.Type.INFO, link=''):
    return Notification.objects.create(recipient=recipient, title=title, message=message[:500], kind=kind,
                                       type=type, link=link)


def notify_admins(title, message, kind, type=Notification.Type.INFO, link='', roles=ADMIN_ROLES, exclude=None):
    """Tell every active admin with one of `roles` (except `exclude`, usually whoever acted)."""
    admins = User.objects.filter(role__in=roles, status=Status.ACTIVE)
    if exclude is not None:
        admins = admins.exclude(pk=exclude.pk)
    Notification.objects.bulk_create([
        Notification(recipient=admin, title=title, message=message[:500], kind=kind, type=type, link=link)
        for admin in admins
    ])


def customer_link(txn):
    return f'/customer/transactions/{txn.reference}'


def payment_review_link(txn):
    return f'/admin/payments/{txn.reference}'

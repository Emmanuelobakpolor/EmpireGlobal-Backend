"""Server-side audit trail. Call `record` from the view that performs the action."""

from .models import AuditLog

ROLE_LABELS = {'super_admin': 'Super Admin', 'admin': 'Admin', 'customer': 'Customer'}


def naira(amount):
    return f'₦{float(amount):,.0f}'


def record(actor, action, details='', reference='', status=AuditLog.Status.INFO, agent_code=''):
    return AuditLog.objects.create(
        actor=actor if actor and actor.pk else None,
        actor_name=getattr(actor, 'full_name', '') or 'System',
        actor_role=ROLE_LABELS.get(getattr(actor, 'role', ''), ''),
        action=action,
        reference=str(reference)[:200],
        agent_code=agent_code or '',
        status=status,
        details=details,
    )

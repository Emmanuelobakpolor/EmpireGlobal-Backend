from django.contrib import admin

from .models import Notification, SlipEvent, Transaction, Withdrawal, WithdrawalEvent


class SlipEventInline(admin.TabularInline):
    model = SlipEvent
    extra = 0
    readonly_fields = ['action', 'actor_name', 'actor_role', 'note', 'at']
    can_delete = False


@admin.register(Transaction)
class TransactionAdmin(admin.ModelAdmin):
    list_display = ['reference', 'customer', 'product_name', 'amount', 'status', 'created_at']
    list_filter = ['status', 'product_type']
    search_fields = ['reference', 'customer__email', 'customer__full_name', 'customer__public_id']
    # Status and amounts change only through the review flow, which credits balances
    readonly_fields = [f.name for f in Transaction._meta.fields]
    inlines = [SlipEventInline]

    def has_add_permission(self, request):
        return False


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ['recipient', 'title', 'kind', 'type', 'read', 'created_at']
    list_filter = ['kind', 'type', 'read']


class WithdrawalEventInline(admin.TabularInline):
    model = WithdrawalEvent
    extra = 0
    readonly_fields = ['action', 'actor_name', 'actor_role', 'note', 'at']
    can_delete = False


@admin.register(Withdrawal)
class WithdrawalAdmin(admin.ModelAdmin):
    list_display = ['reference', 'customer', 'amount', 'payout_amount', 'status', 'created_at']
    list_filter = ['status', 'kind']
    search_fields = ['reference', 'customer__email', 'customer__full_name', 'account_number']
    # Status and amounts change only through the review flow, which moves balances
    readonly_fields = [f.name for f in Withdrawal._meta.fields]
    inlines = [WithdrawalEventInline]

    def has_add_permission(self, request):
        return False

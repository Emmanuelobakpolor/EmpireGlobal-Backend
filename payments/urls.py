from django.urls import path

from . import views

urlpatterns = [
    path('transactions/', views.TransactionListView.as_view()),
    path('transactions/<str:reference>/', views.TransactionDetailView.as_view()),
    path('transactions/<str:reference>/receipt/', views.ReceiptFileView.as_view()),
    path('transactions/<str:reference>/upload-receipt/', views.UploadReceiptView.as_view()),
    path('transactions/<str:reference>/documents/<str:key>/', views.DocumentFileView.as_view()),
    path('notifications/', views.NotificationListView.as_view()),
    path('notifications/read-all/', views.NotificationReadView.as_view()),
    path('notifications/<int:pk>/read/', views.NotificationReadView.as_view()),
    path('admin/transactions/', views.AdminTransactionListView.as_view()),
    path('admin/transactions/<str:reference>/', views.AdminTransactionView.as_view()),
    path('admin/transactions/<str:reference>/view/', views.RecordViewView.as_view()),
    path('admin/transactions/<str:reference>/recommend/', views.RecommendView.as_view()),
    path('admin/transactions/<str:reference>/approve/', views.ApproveView.as_view()),
    path('admin/transactions/<str:reference>/reject/', views.RejectView.as_view()),
    path('admin/bank-accounts/', views.BankAccountListView.as_view()),
    path('admin/bank-accounts/assignments/<str:facility>/', views.AssignmentView.as_view()),
    path('admin/bank-accounts/<str:code>/', views.BankAccountDetailView.as_view()),
    path('admin/audit-logs/', views.AuditLogListView.as_view()),
    path('withdrawals/', views.WithdrawalListView.as_view()),
    path('withdrawals/plans/', views.WithdrawablePlansView.as_view()),
    path('withdrawals/quote/', views.WithdrawalQuoteView.as_view()),
    path('withdrawals/<str:reference>/cancel/', views.CancelWithdrawalView.as_view()),
    path('admin/withdrawals/', views.AdminWithdrawalListView.as_view()),
    path('admin/withdrawals/<str:reference>/', views.AdminWithdrawalView.as_view()),
    path('admin/withdrawals/<str:reference>/recommend/', views.AdminWithdrawalActionView.as_view(action_name='recommend')),
    path('admin/withdrawals/<str:reference>/approve/', views.AdminWithdrawalActionView.as_view(action_name='approve')),
    path('admin/withdrawals/<str:reference>/reject/', views.AdminWithdrawalActionView.as_view(action_name='reject')),
    path('admin/withdrawals/<str:reference>/mark-paid/', views.AdminWithdrawalActionView.as_view(action_name='mark-paid')),
]

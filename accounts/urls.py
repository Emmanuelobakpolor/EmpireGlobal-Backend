from django.urls import path

from . import admin_views, views

urlpatterns = [
    path('setup/super-admin/', views.SetupSuperAdminView.as_view()),
    path('auth/csrf/', views.CsrfView.as_view()),
    path('auth/register/', views.RegisterView.as_view()),
    path('auth/verify-email/', views.VerifyEmailView.as_view()),
    path('auth/resend-otp/', views.ResendOtpView.as_view()),
    path('auth/login/', views.LoginView.as_view()),
    path('auth/google/', views.GoogleSignInView.as_view()),
    path('auth/google/complete/', views.GoogleCompleteSignupView.as_view()),
    path('auth/logout/', views.LogoutView.as_view()),
    path('auth/me/', views.MeView.as_view()),
    path('auth/me/avatar/', views.AvatarView.as_view()),
    path('users/<str:public_id>/avatar/', views.AvatarFileView.as_view()),
    path('auth/change-password/', views.ChangePasswordView.as_view()),
    path('auth/change-email/', views.ChangeEmailView.as_view()),
    path('auth/change-email/resend/', views.ResendChangeEmailView.as_view()),
    path('auth/change-email/confirm/', views.ConfirmChangeEmailView.as_view()),
    path('auth/password-reset/', views.PasswordResetRequestView.as_view()),
    path('auth/password-reset/confirm/', views.PasswordResetConfirmView.as_view()),
    path('admin/auth/login/', views.AdminLoginView.as_view()),
    path('admin/auth/verify-code/', views.AdminVerifyTwoFactorView.as_view()),
    path('admin/auth/resend-code/', views.AdminResendTwoFactorView.as_view()),
    path('admin/admins/', admin_views.AdminListView.as_view()),
    path('admin/admins/<str:public_id>/', admin_views.AdminDetailView.as_view()),
    path('admin/customers/', admin_views.CustomerListView.as_view()),
    path('admin/customers/<str:public_id>/', admin_views.CustomerDetailView.as_view()),
]

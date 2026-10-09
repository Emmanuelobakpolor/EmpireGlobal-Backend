from django.conf import settings
from django.core.mail import send_mail


def send_verification_code(user, code):
    minutes = settings.OTP_TTL_SECONDS // 60
    send_mail(
        subject='Your Empire Global verification code',
        message=(
            f'Hi {user.full_name},\n\n'
            f'Your Empire Global verification code is {code}.\n'
            f'It expires in {minutes} minutes. If you did not sign up, you can ignore this email.\n'
        ),
        from_email=None,
        recipient_list=[user.email],
    )


def send_password_reset_link(user, link):
    send_mail(
        subject='Reset your Empire Global password',
        message=(
            f'Hi {user.full_name},\n\n'
            f'Use the link below to choose a new password:\n{link}\n\n'
            'If you did not request a password reset, you can ignore this email.\n'
        ),
        from_email=None,
        recipient_list=[user.email],
    )


def send_admin_login_code(user, code):
    minutes = settings.OTP_TTL_SECONDS // 60
    send_mail(
        subject='Your Empire Global admin sign-in code',
        message=(
            f'Hi {user.full_name},\n\n'
            f'Your admin sign-in code is {code}.\n'
            f'It expires in {minutes} minutes.\n\n'
            'If you did not just sign in, someone may know your password. Change it now and tell a Super Admin.\n'
        ),
        from_email=None,
        recipient_list=[user.email],
    )


def send_email_change_code(user, new_email, code):
    minutes = settings.OTP_TTL_SECONDS // 60
    send_mail(
        subject='Confirm your new Empire Global email',
        message=(
            f'Hi {user.full_name},\n\n'
            f'Your code to confirm this as your new email address is {code}.\n'
            f'It expires in {minutes} minutes. If you did not ask for this, you can ignore this email.\n'
        ),
        from_email=None,
        recipient_list=[new_email],
    )


def send_email_change_requested(user, new_email):
    send_mail(
        subject='Your Empire Global email is being changed',
        message=(
            f'Hi {user.full_name},\n\n'
            f'Someone asked to change the email on your Empire Global account to {new_email}.\n'
            'Nothing changes until the new address is confirmed.\n\n'
            'If this was not you, change your password now and contact support.\n'
        ),
        from_email=None,
        recipient_list=[user.email],
    )


def send_email_changed(user, old_email):
    send_mail(
        subject='Your Empire Global email was changed',
        message=(
            f'Hi {user.full_name},\n\n'
            f'The email on your Empire Global account was changed from {old_email} to {user.email}.\n'
            'You will now sign in with the new address.\n\n'
            'If this was not you, contact support immediately.\n'
        ),
        from_email=None,
        recipient_list=[old_email],
    )

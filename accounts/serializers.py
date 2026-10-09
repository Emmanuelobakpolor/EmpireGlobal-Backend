from django.contrib.auth import password_validation
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers

from core.models import Agent
from payments.files import validate_upload

from .models import Role, Status, User, normalize_agent_code


NEXT_OF_KIN_FIELDS = {
    'fullName': 'Full name is required.',
    'phone': 'Phone number is required.',
    'relationship': 'Relationship is required.',
    'address': 'Address is required.',
}


class NextOfKinField(serializers.JSONField):
    """Exactly the four next-of-kin fields, each a non-empty string."""

    def to_internal_value(self, data):
        if data is None:
            return None
        if not isinstance(data, dict):
            raise serializers.ValidationError('Next of kin must be an object.')
        errors, value = {}, {}
        for key, message in NEXT_OF_KIN_FIELDS.items():
            item = data.get(key)
            if not isinstance(item, str) or not item.strip():
                errors[key] = message
            elif len(item) > 255:
                errors[key] = 'Must be 255 characters or fewer.'
            else:
                value[key] = item.strip()
        if errors:
            raise serializers.ValidationError(errors)
        return value


def avatar_url(user):
    """Where the app loads a user's display picture from, or None when they haven't set one."""
    if not user.avatar:
        return None
    storage = user.avatar.storage
    if hasattr(storage, 'avatar_url'):
        # Cloudinary: a signed, face-cropped link straight to the CDN
        return storage.avatar_url(user.avatar.name)
    # Local storage: streamed by the API; the version busts the browser cache after a change
    version = int(user.avatar_updated_at.timestamp()) if user.avatar_updated_at else 0
    return f'/api/users/{user.public_id}/avatar/?v={version}'


class UserSerializer(serializers.ModelSerializer):
    """The signed-in user, in the camelCase shape the React app already uses."""

    id = serializers.CharField(source='public_id', read_only=True)
    fullName = serializers.CharField(source='full_name', max_length=150)
    agentCode = serializers.CharField(source='agent_code', read_only=True)
    authProvider = serializers.CharField(source='auth_provider', read_only=True)
    emailVerified = serializers.BooleanField(source='email_verified', read_only=True)
    joined = serializers.DateTimeField(source='date_joined', format='%Y-%m-%d', read_only=True)
    nextOfKin = NextOfKinField(source='next_of_kin', required=False, allow_null=True)
    mustChangePassword = serializers.BooleanField(source='must_change_password', read_only=True)
    # Balances change only when a Super Admin approves a payment
    savingsBalance = serializers.FloatField(source='savings_balance', read_only=True)
    investmentBalance = serializers.FloatField(source='investment_balance', read_only=True)
    outstandingLoan = serializers.FloatField(source='outstanding_loan', read_only=True)
    totalBalance = serializers.SerializerMethodField()
    avatarUrl = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = ['id', 'fullName', 'email', 'phone', 'agentCode', 'role', 'status', 'authProvider', 'emailVerified',
                  'joined', 'nextOfKin', 'mustChangePassword', 'savingsBalance', 'investmentBalance', 'outstandingLoan',
                  'totalBalance', 'avatarUrl']
        # Email changes go through the confirmation-code flow; role and status are set by admins
        read_only_fields = ['email', 'role', 'status']

    def get_totalBalance(self, user):
        # As shown in the app: what the customer holds, not counting loans
        return float(user.savings_balance + user.investment_balance)

    def get_avatarUrl(self, user):
        return avatar_url(user)

    def validate_fullName(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError('Full name is required.')
        return value


class RegisterSerializer(serializers.Serializer):
    fullName = serializers.CharField(max_length=150)
    email = serializers.EmailField()
    phone = serializers.CharField(max_length=32)
    agentCode = serializers.CharField(max_length=16, required=False, allow_blank=True)
    password = serializers.CharField(write_only=True, trim_whitespace=False)

    def validate_fullName(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError('Full name is required.')
        return value

    def validate_email(self, value):
        return value.strip().lower()

    def validate_phone(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError('Phone number is required.')
        return value

    def validate_agentCode(self, value):
        # Optional, but if given it must belong to an active agent
        code = normalize_agent_code(value)
        if not code:
            return ''
        agent = Agent.objects.filter(code=code).first()
        if agent is None:
            raise serializers.ValidationError('Agent code not found. Check the code with your agent, or leave it blank.')
        if not agent.is_active:
            raise serializers.ValidationError('This agent code is no longer active. Check with your agent, or leave it blank.')
        return code

    def validate(self, attrs):
        candidate = User(email=attrs['email'], full_name=attrs['fullName'])
        try:
            password_validation.validate_password(attrs['password'], candidate)
        except DjangoValidationError as exc:
            raise serializers.ValidationError({'password': list(exc.messages)})
        return attrs


class EmailSerializer(serializers.Serializer):
    email = serializers.EmailField()

    def validate_email(self, value):
        return value.strip().lower()


class VerifyEmailSerializer(EmailSerializer):
    code = serializers.RegexField(r'^\d+$', max_length=12)


class CodeSerializer(serializers.Serializer):
    code = serializers.RegexField(r'^\d+$', max_length=12)


class ChangeEmailSerializer(serializers.Serializer):
    newEmail = serializers.EmailField()
    password = serializers.CharField(trim_whitespace=False)

    def validate_newEmail(self, value):
        return value.strip().lower()


class LoginSerializer(EmailSerializer):
    password = serializers.CharField(trim_whitespace=False)


class PasswordResetConfirmSerializer(serializers.Serializer):
    uid = serializers.CharField()
    token = serializers.CharField()
    password = serializers.CharField(trim_whitespace=False)


class ChangePasswordSerializer(serializers.Serializer):
    currentPassword = serializers.CharField(trim_whitespace=False)
    newPassword = serializers.CharField(trim_whitespace=False)


class CustomerAccountSerializer(UserSerializer):
    """A customer as admins see them. Admins may only change the status."""

    status = serializers.ChoiceField(choices=Status.choices)

    def get_fields(self):
        # Meta.read_only_fields skips explicitly declared fields, so lock them here
        fields = super().get_fields()
        for name, field in fields.items():
            if name != 'status':
                field.read_only = True
        return fields

    def validate_status(self, value):
        if self.instance.status == Status.PENDING:
            raise serializers.ValidationError('This customer has not verified their email yet.')
        if value not in (Status.ACTIVE, Status.SUSPENDED):
            raise serializers.ValidationError('Status must be active or suspended.')
        return value


class AdminAccountSerializer(serializers.ModelSerializer):
    id = serializers.CharField(source='public_id', read_only=True)
    fullName = serializers.CharField(source='full_name', max_length=150)
    # Declared so validate_email (case-insensitive, friendlier message) replaces the model's unique check
    email = serializers.EmailField()
    role = serializers.ChoiceField(choices=[Role.ADMIN, Role.SUPER_ADMIN])
    status = serializers.ChoiceField(choices=[Status.ACTIVE, Status.INACTIVE], required=False)
    password = serializers.CharField(write_only=True, required=False, allow_blank=True, trim_whitespace=False)
    createdAt = serializers.DateTimeField(source='date_joined', format='%Y-%m-%d', read_only=True)
    avatarUrl = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = ['id', 'fullName', 'email', 'role', 'status', 'password', 'createdAt', 'avatarUrl']

    def get_avatarUrl(self, user):
        return avatar_url(user)

    def validate_fullName(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError('Full name is required.')
        return value

    def validate_email(self, value):
        value = value.strip().lower()
        taken = User.objects.filter(email=value)
        if self.instance:
            taken = taken.exclude(pk=self.instance.pk)
        if taken.exists():
            raise serializers.ValidationError('An account with this email already exists.')
        return value

    def validate(self, attrs):
        acting = self.context['request'].user
        password = attrs.pop('password', '')
        if self.instance is None and not password:
            raise serializers.ValidationError({'password': ['A temporary password is required.']})
        if self.instance is not None and self.instance.pk == acting.pk:
            # A Super Admin can't demote or deactivate themselves and lock everyone out
            if attrs.get('role', self.instance.role) != self.instance.role:
                raise serializers.ValidationError({'role': ['You cannot change your own role.']})
            if attrs.get('status', self.instance.status) != self.instance.status:
                raise serializers.ValidationError({'status': ['You cannot deactivate your own account.']})
        if password:
            candidate = self.instance or User(email=attrs.get('email', ''), full_name=attrs.get('full_name', ''))
            try:
                password_validation.validate_password(password, candidate)
            except DjangoValidationError as exc:
                raise serializers.ValidationError({'password': list(exc.messages)})
            attrs['password'] = password
        return attrs

    def create(self, validated_data):
        password = validated_data.pop('password')
        validated_data.pop('status', None)
        # Staff-created accounts skip email verification and start active, but the
        # admin must replace the temporary password before they can do anything else
        return User.objects.create_user(
            password=password, status=Status.ACTIVE, email_verified=True, must_change_password=True, **validated_data,
        )

    def update(self, instance, validated_data):
        password = validated_data.pop('password', None)
        for key, value in validated_data.items():
            setattr(instance, key, value)
        if password:
            # Signs the admin out of existing sessions. A password set for someone
            # else is temporary: they must choose their own at next sign-in.
            instance.set_password(password)
            instance.must_change_password = instance.pk != self.context['request'].user.pk
        instance.save()
        return instance


class AvatarSerializer(serializers.Serializer):
    avatar = serializers.FileField()

    def validate_avatar(self, file):
        return validate_upload(file, images_only=True)


class SetupSuperAdminSerializer(serializers.Serializer):
    email = serializers.EmailField()
    fullName = serializers.CharField(max_length=150)
    password = serializers.CharField(write_only=True, trim_whitespace=False)

    def validate_email(self, value):
        value = value.strip().lower()
        if User.objects.filter(email=value).exists():
            raise serializers.ValidationError('An account with this email already exists.')
        return value

    def validate_fullName(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError('Full name is required.')
        return value

    def validate(self, attrs):
        candidate = User(email=attrs['email'], full_name=attrs['fullName'])
        try:
            password_validation.validate_password(attrs['password'], candidate)
        except DjangoValidationError as exc:
            raise serializers.ValidationError({'password': list(exc.messages)})
        return attrs

from rest_framework import serializers

from .models import Agent, PlatformSettings

TIMEZONES = ['Africa/Lagos', 'UTC']


def required_text(value, message):
    value = value.strip()
    if not value:
        raise serializers.ValidationError(message)
    return value


class AgentSerializer(serializers.ModelSerializer):
    createdAt = serializers.DateTimeField(source='created_at', format='%Y-%m-%d', read_only=True)

    class Meta:
        model = Agent
        fields = ['code', 'name', 'phone', 'location', 'status', 'createdAt']
        read_only_fields = ['code']

    def validate_name(self, value):
        return required_text(value, 'Agent name is required.')

    def validate_phone(self, value):
        return required_text(value, 'Phone number is required.')

    def validate_location(self, value):
        return required_text(value, 'Location is required.')


class PlatformSettingsSerializer(serializers.ModelSerializer):
    platformName = serializers.CharField(source='platform_name', max_length=100)
    supportEmail = serializers.EmailField(source='support_email')
    supportPhone = serializers.CharField(source='support_phone', max_length=32)
    timezone = serializers.ChoiceField(choices=TIMEZONES)
    updatedAt = serializers.DateTimeField(source='updated_at', read_only=True)

    class Meta:
        model = PlatformSettings
        fields = ['platformName', 'supportEmail', 'supportPhone', 'timezone', 'updatedAt']

    def validate_platformName(self, value):
        return required_text(value, 'Platform name is required.')

    def validate_supportPhone(self, value):
        return required_text(value, 'Support phone is required.')


class PublicSettingsSerializer(serializers.ModelSerializer):
    """What anyone may read: the name and how to contact support."""

    platformName = serializers.CharField(source='platform_name')
    supportEmail = serializers.EmailField(source='support_email')
    supportPhone = serializers.CharField(source='support_phone')

    class Meta:
        model = PlatformSettings
        fields = ['platformName', 'supportEmail', 'supportPhone']

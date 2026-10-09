from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.permissions import AllowAny, SAFE_METHODS
from rest_framework.response import Response

from accounts.permissions import IsAdmin, IsSuperAdmin
from accounts.views import CsrfAPIView

from . import audit
from .models import Agent, PlatformSettings, normalize_agent_code
from .serializers import AgentSerializer, PlatformSettingsSerializer, PublicSettingsSerializer


class AgentListView(CsrfAPIView):
    permission_classes = [IsAdmin]

    def get(self, request):
        return Response({'agents': AgentSerializer(Agent.objects.all(), many=True).data})

    def post(self, request):
        serializer = AgentSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        # New agents always start active
        agent = serializer.save(status=Agent.Status.ACTIVE)
        audit.record(request.user, 'Agent Created', f'Created agent {agent.name} ({agent.code}) in {agent.location}.',
                     reference=agent.code, agent_code=agent.code, status='success')
        return Response({'agent': AgentSerializer(agent).data}, status=status.HTTP_201_CREATED)


class AgentDetailView(CsrfAPIView):
    permission_classes = [IsAdmin]

    def patch(self, request, code):
        agent = get_object_or_404(Agent, code=normalize_agent_code(code))
        before = agent.status
        serializer = AgentSerializer(agent, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        agent = serializer.save()
        if agent.status != before:
            audit.record(request.user, 'Agent Activated' if agent.is_active else 'Agent Deactivated',
                         f'Agent {agent.name} ({agent.code}) was {"activated" if agent.is_active else "deactivated"}.',
                         reference=agent.code, agent_code=agent.code, status='info' if agent.is_active else 'error')
        if set(request.data) - {'status'}:
            audit.record(request.user, 'Agent Updated', f'Updated agent {agent.name} ({agent.code}).',
                         reference=agent.code, agent_code=agent.code)
        return Response({'agent': serializer.data})


class SuperAdminWrites(IsAdmin):
    """Any admin can read; only Super Admins can change."""

    def has_permission(self, request, view):
        if request.method in SAFE_METHODS:
            return super().has_permission(request, view)
        return IsSuperAdmin().has_permission(request, view)


class PlatformSettingsView(CsrfAPIView):
    permission_classes = [SuperAdminWrites]

    def get(self, request):
        return Response({'settings': PlatformSettingsSerializer(PlatformSettings.load()).data})

    def patch(self, request):
        serializer = PlatformSettingsSerializer(PlatformSettings.load(), data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        changed = ', '.join(sorted(k for k in request.data if k in serializer.fields)) or 'nothing'
        audit.record(request.user, 'Settings Updated', f'Platform settings were updated ({changed}).',
                     reference='General Settings')
        return Response({'settings': serializer.data})


class PublicSettingsView(CsrfAPIView):
    permission_classes = [AllowAny]

    def get(self, request):
        return Response({'settings': PublicSettingsSerializer(PlatformSettings.load()).data})

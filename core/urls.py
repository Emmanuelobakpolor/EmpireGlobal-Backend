from django.urls import path

from . import views

urlpatterns = [
    path('settings/', views.PublicSettingsView.as_view()),
    path('admin/settings/', views.PlatformSettingsView.as_view()),
    path('admin/agents/', views.AgentListView.as_view()),
    path('admin/agents/<str:code>/', views.AgentDetailView.as_view()),
]

from django.urls import path

from . import views

urlpatterns = [
    path('products/', views.PublicProductListView.as_view()),
    path('admin/products/', views.AdminProductListView.as_view()),
    path('admin/products/<str:code>/', views.AdminProductDetailView.as_view()),
]

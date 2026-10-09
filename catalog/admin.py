from django.contrib import admin

from .models import Product


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ['code', 'name', 'type', 'min_amount', 'max_amount', 'status', 'position']
    list_filter = ['type', 'status']
    search_fields = ['code', 'name', 'category']

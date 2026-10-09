from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from accounts.permissions import IsAdmin
from accounts.views import CsrfAPIView, error
from core import audit
from payments.models import Transaction

from .models import Product
from .serializers import ProductSerializer


class PublicProductListView(CsrfAPIView):
    """The live catalogue for the website and the customer portal."""

    permission_classes = [AllowAny]

    def get(self, request):
        products = Product.objects.filter(status=Product.Status.ACTIVE)
        return Response({'products': ProductSerializer(products, many=True).data})


class AdminProductListView(CsrfAPIView):
    permission_classes = [IsAdmin]

    def get(self, request):
        return Response({'products': ProductSerializer(Product.objects.all(), many=True).data})

    def post(self, request):
        serializer = ProductSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        product = serializer.save()
        audit.record(request.user, 'Product Created', f'New {product.type} product "{product.name}" added.',
                     reference=product.name, status='success')
        return Response({'product': ProductSerializer(product).data}, status=status.HTTP_201_CREATED)


class AdminProductDetailView(CsrfAPIView):
    permission_classes = [IsAdmin]

    def patch(self, request, code):
        product = get_object_or_404(Product, code=code)
        before = product.status
        serializer = ProductSerializer(product, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        product = serializer.save()
        if set(request.data) == {'status'}:
            if before != product.status:
                audit.record(request.user, 'Product Enabled' if product.is_active else 'Product Disabled',
                             f'Product "{product.name}" is now {product.status}.', reference=product.name,
                             status='info' if product.is_active else 'warning')
        else:
            audit.record(request.user, 'Product Updated', f'Product "{product.name}" was updated.', reference=product.name)
        return Response({'product': ProductSerializer(product).data})

    def delete(self, request, code):
        product = get_object_or_404(Product, code=code)
        # Keep products customers have used, so their limits and terms stay on record
        if Transaction.objects.filter(product_id=product.code).exists():
            return error('Customers already have transactions on this product. Disable it instead of deleting.',
                         'product_in_use')
        name = product.name
        product.delete()
        audit.record(request.user, 'Product Deleted', f'Product "{name}" was deleted.', reference=name, status='error')
        return Response(status=status.HTTP_204_NO_CONTENT)

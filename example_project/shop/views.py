"""A minimal public product page: the target of Gozar's "View on site" link."""

from django.shortcuts import get_object_or_404, render

from shop.models import Product


def product(request, pk):
    obj = get_object_or_404(Product.objects.filter(is_active=True), pk=pk)
    return render(request, "shop/product.html", {"product": obj, "images": obj.images.all()})

from django.contrib import admin

from .models import Bundle, Loan, Location, Product, ProfitPartner, Sale, StoreConfig, Vendor


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ("barcode", "name")
    search_fields = ("barcode", "name")


admin.site.register(Location)
admin.site.register(Vendor)
admin.site.register(Bundle)
admin.site.register(StoreConfig)
admin.site.register(Sale)
admin.site.register(ProfitPartner)
admin.site.register(Loan)

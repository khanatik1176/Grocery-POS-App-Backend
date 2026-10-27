from django.conf import settings
from django.db import models


class Location(models.Model):
    name = models.CharField(primary_key=True, max_length=80)
    kind = models.CharField(
        max_length=20,
        choices=[("warehouse", "Warehouse"), ("shop", "Shop"), ("other", "Other")],
    )

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class Vendor(models.Model):
    name = models.CharField(primary_key=True, max_length=120)
    phone = models.CharField(max_length=30, blank=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class Product(models.Model):
    """A product is identified by the barcode or QR already printed on it."""

    barcode = models.CharField(primary_key=True, max_length=64)
    name = models.CharField(max_length=200)
    image = models.ImageField(upload_to="products/", blank=True)
    reorder_level = models.PositiveIntegerField(null=True, blank=True)
    lead_days = models.PositiveIntegerField(null=True, blank=True)
    notify_enabled = models.BooleanField(null=True, blank=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class StockLot(models.Model):
    """One physical batch.

    The same barcode can arrive with different dates, prices, or sources.
    That combination is the identity of the batch. Nothing extra is generated.
    """

    pk = models.CompositePrimaryKey(
        "product",
        "manufacturing_date",
        "expiry_date",
        "mrp_paisa",
        "purchase_paisa",
        "discount_bps",
        "origin",
        "location",
    )
    product = models.ForeignKey(Product, related_name="lots", on_delete=models.CASCADE)
    manufacturing_date = models.DateField()
    expiry_date = models.DateField()
    mrp_paisa = models.PositiveIntegerField()
    purchase_paisa = models.PositiveIntegerField()
    discount_bps = models.PositiveIntegerField()
    origin = models.ForeignKey(Vendor, related_name="lots", on_delete=models.PROTECT)
    location = models.ForeignKey(Location, related_name="lots", on_delete=models.PROTECT)
    quantity = models.PositiveIntegerField(default=0)

    def __str__(self):
        return f"{self.product_id} @ {self.location_id}"


class StockMove(models.Model):
    kind = models.CharField(
        max_length=16,
        default="receive",
        choices=[("receive", "Receive"), ("sale", "Sale"), ("transfer", "Transfer")],
    )
    sale = models.ForeignKey(
        "Sale",
        null=True,
        blank=True,
        related_name="moves",
        on_delete=models.PROTECT,
    )
    product = models.ForeignKey(Product, related_name="moves", on_delete=models.PROTECT)
    location = models.ForeignKey(Location, related_name="moves", on_delete=models.PROTECT)
    origin = models.ForeignKey(Vendor, related_name="moves", on_delete=models.PROTECT)
    manufacturing_date = models.DateField()
    expiry_date = models.DateField()
    mrp_paisa = models.PositiveIntegerField()
    purchase_paisa = models.PositiveIntegerField()
    discount_bps = models.PositiveIntegerField()
    quantity = models.IntegerField()
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="stock_moves",
    )
    created_at = models.DateTimeField(auto_now_add=True)


class Bundle(models.Model):
    name = models.CharField(primary_key=True, max_length=120)
    price_paisa = models.PositiveIntegerField()
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class BundleItem(models.Model):
    pk = models.CompositePrimaryKey("bundle", "product")
    bundle = models.ForeignKey(Bundle, related_name="items", on_delete=models.CASCADE)
    product = models.ForeignKey(Product, related_name="bundle_items", on_delete=models.PROTECT)
    quantity = models.PositiveIntegerField()


class StoreConfig(models.Model):
    id = models.PositiveSmallIntegerField(primary_key=True)
    store_name = models.CharField(max_length=80, default="Mudi")
    reorder_level = models.PositiveIntegerField(default=5)
    lead_days = models.PositiveIntegerField(default=7)
    notify_enabled = models.BooleanField(default=True)
    bkash_number = models.CharField(max_length=20, blank=True, default="")
    nagad_number = models.CharField(max_length=20, blank=True, default="")
    rocket_number = models.CharField(max_length=20, blank=True, default="")
    payment_timeout_seconds = models.PositiveIntegerField(default=45)
    sms_enabled = models.BooleanField(default=True)
    email_enabled = models.BooleanField(default=True)
    sms_template = models.CharField(
        max_length=240,
        default="Thank you for shopping with us. Receipt: {url}",
    )
    receipt_base_url = models.CharField(max_length=200, blank=True, default="")

    @classmethod
    def load(cls):
        config, _created = cls.objects.get_or_create(pk=1)
        return config


class ManagerGrant(models.Model):
    """A manager can do a job only after an admin assigns that exact job."""

    pk = models.CompositePrimaryKey("user", "capability")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        related_name="capability_grants",
        on_delete=models.CASCADE,
    )
    capability = models.CharField(max_length=64)


class Sale(models.Model):
    number = models.CharField(primary_key=True, max_length=32)
    location = models.ForeignKey(Location, related_name="sales", on_delete=models.PROTECT)
    cashier = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        related_name="sales",
        on_delete=models.SET_NULL,
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    total_paisa = models.PositiveIntegerField()
    cost_paisa = models.PositiveIntegerField()
    payment_method = models.CharField(max_length=16)
    amount_taken_paisa = models.PositiveIntegerField()
    change_paisa = models.PositiveIntegerField(default=0)
    txn_number = models.CharField(max_length=64, blank=True, default="")
    customer_phone = models.CharField(max_length=20, blank=True, default="")
    customer_email = models.EmailField(blank=True, default="")

    class Meta:
        ordering = ["-created_at"]


class SaleLine(models.Model):
    sale = models.ForeignKey(Sale, related_name="lines", on_delete=models.CASCADE)
    kind = models.CharField(max_length=16)
    name = models.CharField(max_length=200)
    barcode = models.CharField(max_length=64, blank=True, default="")
    quantity = models.PositiveIntegerField()
    unit_price_paisa = models.PositiveIntegerField()
    line_total_paisa = models.PositiveIntegerField()
    cost_paisa = models.PositiveIntegerField(default=0)
    detail = models.CharField(max_length=300, blank=True, default="")


class OutboundNotice(models.Model):
    sale = models.ForeignKey(Sale, related_name="notices", on_delete=models.CASCADE)
    channel = models.CharField(max_length=16)
    destination = models.CharField(max_length=200)
    body = models.TextField()
    mechanism = models.CharField(max_length=32)
    created_at = models.DateTimeField(auto_now_add=True)


class VendorPayment(models.Model):
    vendor = models.ForeignKey(Vendor, related_name="payments", on_delete=models.PROTECT)
    amount_paisa = models.PositiveIntegerField()
    note = models.CharField(max_length=240, blank=True, default="")
    paid_on = models.DateField()
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="vendor_payments",
    )
    created_at = models.DateTimeField(auto_now_add=True)


class ProfitPartner(models.Model):
    name = models.CharField(primary_key=True, max_length=120)
    share_bps = models.PositiveIntegerField()

    class Meta:
        ordering = ["name"]


class Loan(models.Model):
    borrower = models.CharField(max_length=120)
    amount_paisa = models.PositiveIntegerField()
    collected_paisa = models.PositiveIntegerField(default=0)
    opened_on = models.DateField()
    due_on = models.DateField()
    note = models.CharField(max_length=240, blank=True, default="")
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="loans_recorded",
    )

    class Meta:
        ordering = ["due_on", "id"]

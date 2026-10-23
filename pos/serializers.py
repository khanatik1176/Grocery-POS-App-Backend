from rest_framework import serializers


class DocumentSerializer(serializers.Serializer):
    pass


class ReceiveSerializer(serializers.Serializer):
    barcode = serializers.CharField()
    name = serializers.CharField(required=False, allow_blank=True)
    quantity = serializers.IntegerField(min_value=1)
    manufacturing_date = serializers.DateField()
    expiry_date = serializers.DateField()
    mrp = serializers.DecimalField(max_digits=10, decimal_places=2)
    purchase_rate = serializers.DecimalField(max_digits=10, decimal_places=2)
    discount_rate = serializers.DecimalField(max_digits=5, decimal_places=2, required=False, default=0)
    origin = serializers.CharField()
    location = serializers.CharField()
    image_base64 = serializers.CharField(required=False, allow_blank=True)


class PreorderSerializer(serializers.Serializer):
    reorder_level = serializers.IntegerField(min_value=0, allow_null=True, required=False)
    lead_days = serializers.IntegerField(min_value=0, allow_null=True, required=False)
    notify_enabled = serializers.BooleanField(allow_null=True, required=False)


class PreorderDefaultsSerializer(serializers.Serializer):
    reorder_level = serializers.IntegerField(min_value=0)
    lead_days = serializers.IntegerField(min_value=0)
    notify_enabled = serializers.BooleanField()


class LocationSerializer(serializers.Serializer):
    name = serializers.CharField()
    kind = serializers.ChoiceField(choices=["warehouse", "shop", "other"])


class BundleItemSerializer(serializers.Serializer):
    barcode = serializers.CharField()
    quantity = serializers.IntegerField(min_value=1)


class BundleSerializer(serializers.Serializer):
    name = serializers.CharField()
    price = serializers.DecimalField(max_digits=10, decimal_places=2)
    active = serializers.BooleanField(required=False, default=True)
    items = BundleItemSerializer(many=True)


class StoreSettingsSerializer(serializers.Serializer):
    store_name = serializers.CharField(required=False)
    bkash_number = serializers.CharField(required=False, allow_blank=True)
    nagad_number = serializers.CharField(required=False, allow_blank=True)
    rocket_number = serializers.CharField(required=False, allow_blank=True)
    payment_timeout_seconds = serializers.IntegerField(required=False)
    sms_enabled = serializers.BooleanField(required=False)
    email_enabled = serializers.BooleanField(required=False)
    sms_template = serializers.CharField(required=False)
    receipt_base_url = serializers.CharField(required=False, allow_blank=True)


class SaleLineSerializer(serializers.Serializer):
    barcode = serializers.CharField(required=False, allow_blank=True)
    bundle = serializers.CharField(required=False, allow_blank=True)
    quantity = serializers.IntegerField(min_value=1)


class QuoteSerializer(serializers.Serializer):
    location = serializers.CharField()
    lines = SaleLineSerializer(many=True)


class PaymentSerializer(serializers.Serializer):
    method = serializers.ChoiceField(choices=["cash", "bkash", "nagad", "rocket"])
    amount_taken = serializers.DecimalField(max_digits=10, decimal_places=2)
    txn_number = serializers.CharField(required=False, allow_blank=True)


class CheckoutSerializer(serializers.Serializer):
    location = serializers.CharField()
    lines = SaleLineSerializer(many=True)
    payment = PaymentSerializer()
    customer_phone = serializers.CharField(required=False, allow_blank=True)
    customer_email = serializers.EmailField(required=False, allow_blank=True)


class SmsSerializer(serializers.Serializer):
    text = serializers.CharField()


class VendorPaymentSerializer(serializers.Serializer):
    vendor = serializers.CharField()
    amount = serializers.DecimalField(max_digits=10, decimal_places=2)
    paid_on = serializers.DateField()
    note = serializers.CharField(required=False, allow_blank=True)
    phone = serializers.CharField(required=False, allow_blank=True)


class PartnerSerializer(serializers.Serializer):
    name = serializers.CharField()
    share = serializers.DecimalField(max_digits=5, decimal_places=2)


class LoanSerializer(serializers.Serializer):
    borrower = serializers.CharField()
    amount = serializers.DecimalField(max_digits=10, decimal_places=2)
    opened_on = serializers.DateField()
    due_on = serializers.DateField()
    note = serializers.CharField(required=False, allow_blank=True)


class CollectSerializer(serializers.Serializer):
    amount = serializers.DecimalField(max_digits=10, decimal_places=2)


class SummaryQuerySerializer(serializers.Serializer):
    preset = serializers.ChoiceField(
        choices=["today", "date", "week", "week_range", "days30", "month", "range"],
        default="today",
    )
    date = serializers.DateField(required=False)
    start = serializers.DateField(required=False)
    end = serializers.DateField(required=False)


class GrantSerializer(serializers.Serializer):
    capabilities = serializers.ListField(child=serializers.CharField(), allow_empty=True)

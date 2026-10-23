from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import extend_schema
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from api.permissions import IsAdminRole

from .capabilities import KNOWN, catalog
from .models import Sale
from .permissions import require
from .serializers import (
    BundleSerializer,
    CheckoutSerializer,
    CollectSerializer,
    DocumentSerializer,
    GrantSerializer,
    LoanSerializer,
    LocationSerializer,
    PartnerSerializer,
    PreorderDefaultsSerializer,
    PreorderSerializer,
    QuoteSerializer,
    ReceiveSerializer,
    SmsSerializer,
    StoreSettingsSerializer,
    SummaryQuerySerializer,
    VendorPaymentSerializer,
)
from .services import (
    accounting_overview,
    bundle_rows,
    catalog as sales_catalog,
    checkout,
    collect_loan,
    create_loan,
    create_location,
    delete_bundle,
    delete_location,
    delete_partner,
    location_rows,
    parse_payment_sms,
    pay_vendor,
    payment_config,
    plan_payload,
    preorder_defaults,
    product_detail,
    product_rows,
    receipt_html,
    receive_stock,
    save_bundle,
    save_partner,
    store_settings,
    summary_for,
    update_preorder,
    update_preorder_defaults,
    update_store_settings,
    build_plan,
)
from django.contrib.auth import get_user_model

User = get_user_model()


class PosView(APIView):
    serializer_class = DocumentSerializer


class ProductListView(PosView):
    @extend_schema(tags=["inventory"], operation_id="inventory_products_list")
    def get(self, request):
        require(request.user, "inventory.view")
        return Response(product_rows(request.query_params.get("q", "")))


class ProductDetailView(PosView):
    @extend_schema(tags=["inventory"], operation_id="inventory_product_retrieve")
    def get(self, request, barcode):
        require(request.user, "inventory.view", "inventory.receive", "inventory.preorder")
        return Response(product_detail(barcode))

    @extend_schema(tags=["inventory"], request=PreorderSerializer)
    def patch(self, request, barcode):
        require(request.user, "inventory.preorder")
        serializer = PreorderSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return Response(update_preorder(barcode, serializer.validated_data))


class ReceiveView(PosView):
    @extend_schema(tags=["inventory"], request=ReceiveSerializer)
    def post(self, request):
        require(request.user, "inventory.receive")
        serializer = ReceiveSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return Response(receive_stock(request.user, serializer.validated_data), status=201)


class LocationListView(PosView):
    @extend_schema(tags=["inventory"])
    def get(self, request):
        require(
            request.user,
            "inventory.locations",
            "inventory.receive",
            "inventory.view",
            "sales.sell",
        )
        return Response(location_rows())

    @extend_schema(tags=["inventory"], request=LocationSerializer)
    def post(self, request):
        require(request.user, "inventory.locations")
        serializer = LocationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return Response(create_location(serializer.validated_data), status=201)


class LocationDetailView(PosView):
    @extend_schema(tags=["inventory"])
    def delete(self, request, name):
        require(request.user, "inventory.locations")
        delete_location(name)
        return Response(status=204)


class BundleListView(PosView):
    @extend_schema(tags=["inventory"])
    def get(self, request):
        require(request.user, "inventory.bundles", "sales.sell")
        return Response(bundle_rows())

    @extend_schema(tags=["inventory"], request=BundleSerializer)
    def post(self, request):
        require(request.user, "inventory.bundles")
        serializer = BundleSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return Response(save_bundle(serializer.validated_data), status=201)


class BundleDetailView(PosView):
    @extend_schema(tags=["inventory"], request=BundleSerializer)
    def put(self, request, name):
        require(request.user, "inventory.bundles")
        serializer = BundleSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return Response(save_bundle(serializer.validated_data, replacing=name))

    @extend_schema(tags=["inventory"])
    def delete(self, request, name):
        require(request.user, "inventory.bundles")
        delete_bundle(name)
        return Response(status=204)


class PreorderDefaultsView(PosView):
    @extend_schema(tags=["inventory"])
    def get(self, request):
        require(request.user, "inventory.preorder")
        return Response(preorder_defaults())

    @extend_schema(tags=["inventory"], request=PreorderDefaultsSerializer)
    def patch(self, request):
        require(request.user, "inventory.preorder")
        serializer = PreorderDefaultsSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return Response(update_preorder_defaults(serializer.validated_data))


class StoreSettingsView(PosView):
    @extend_schema(tags=["store"])
    def get(self, request):
        require(request.user, "store.settings")
        return Response(store_settings())

    @extend_schema(tags=["store"], request=StoreSettingsSerializer)
    def patch(self, request):
        require(request.user, "store.settings")
        serializer = StoreSettingsSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        return Response(update_store_settings(serializer.validated_data))


class CatalogView(PosView):
    @extend_schema(tags=["sales"])
    def get(self, request):
        require(request.user, "sales.sell")
        return Response(
            sales_catalog(
                request.query_params.get("q", ""),
                request.query_params.get("location", ""),
            )
        )


class PaymentConfigView(PosView):
    @extend_schema(tags=["sales"])
    def get(self, request):
        require(request.user, "sales.sell")
        return Response(payment_config())


class QuoteView(PosView):
    @extend_schema(tags=["sales"], request=QuoteSerializer)
    def post(self, request):
        require(request.user, "sales.sell")
        serializer = QuoteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        plan = build_plan(serializer.validated_data["location"], serializer.validated_data["lines"])
        return Response(plan_payload(plan))


class CheckoutView(PosView):
    @extend_schema(tags=["sales"], request=CheckoutSerializer)
    def post(self, request):
        require(request.user, "sales.sell")
        serializer = CheckoutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return Response(checkout(request.user, serializer.validated_data, request), status=201)


class ParseSmsView(PosView):
    @extend_schema(tags=["sales"], request=SmsSerializer)
    def post(self, request):
        require(request.user, "sales.sell")
        serializer = SmsSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return Response(parse_payment_sms(serializer.validated_data["text"]))


class ReceiptPageView(PosView):
    authentication_classes = []
    permission_classes = [AllowAny]

    @extend_schema(tags=["sales"])
    def get(self, request, number):
        sale = get_object_or_404(Sale.objects.prefetch_related("lines"), number=number)
        return HttpResponse(receipt_html(sale))


class AccountingView(PosView):
    @extend_schema(tags=["accounting"])
    def get(self, request):
        require(
            request.user,
            "accounting.view",
            "accounting.pay",
            "accounting.profit",
            "accounting.lending",
        )
        return Response(accounting_overview(request.user))


class VendorPaymentView(PosView):
    @extend_schema(tags=["accounting"], request=VendorPaymentSerializer)
    def post(self, request):
        require(request.user, "accounting.pay")
        serializer = VendorPaymentSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return Response(pay_vendor(request.user, serializer.validated_data), status=201)


class PartnerListView(PosView):
    @extend_schema(tags=["accounting"], request=PartnerSerializer)
    def post(self, request):
        require(request.user, "accounting.profit")
        serializer = PartnerSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return Response(save_partner(serializer.validated_data), status=201)


class PartnerDetailView(PosView):
    @extend_schema(tags=["accounting"])
    def delete(self, request, name):
        require(request.user, "accounting.profit")
        delete_partner(name)
        return Response(status=204)


class LoanListView(PosView):
    @extend_schema(tags=["accounting"], request=LoanSerializer)
    def post(self, request):
        require(request.user, "accounting.lending")
        serializer = LoanSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return Response(create_loan(request.user, serializer.validated_data), status=201)


class LoanCollectView(PosView):
    @extend_schema(tags=["accounting"], request=CollectSerializer)
    def post(self, request, loan_id):
        require(request.user, "accounting.lending")
        serializer = CollectSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return Response(collect_loan(loan_id, serializer.validated_data))


class SummaryView(PosView):
    @extend_schema(tags=["summary"])
    def get(self, request):
        require(request.user, "summary.view")
        serializer = SummaryQuerySerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        return Response(summary_for(serializer.validated_data))


class AccessCatalogView(PosView):
    permission_classes = [IsAdminRole]

    @extend_schema(tags=["access"])
    def get(self, request):
        return Response(catalog())


class ManagerAccessView(PosView):
    permission_classes = [IsAdminRole]

    @extend_schema(tags=["access"])
    def get(self, request):
        from .capabilities import capabilities_for

        users = (
            User.objects.filter(profile__isnull=False)
            .exclude(profile__role__slug="admin")
            .select_related("profile__role")
            .order_by("first_name", "email")
        )
        return Response(
            [
                {
                    "id": user.id,
                    "email": user.email,
                    "first_name": user.first_name,
                    "last_name": user.last_name,
                    "role": user.profile.role.name,
                    "capabilities": capabilities_for(user),
                }
                for user in users
            ]
        )


class ManagerGrantView(PosView):
    permission_classes = [IsAdminRole]

    @extend_schema(tags=["access"], request=GrantSerializer)
    def put(self, request, user_id):
        from .models import ManagerGrant

        serializer = GrantSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = (
            User.objects.filter(pk=user_id, profile__isnull=False)
            .exclude(profile__role__slug="admin")
            .first()
        )
        if user is None:
            return Response({"detail": "That person is not a manager."}, status=404)
        codes = list(dict.fromkeys(serializer.validated_data["capabilities"]))
        unknown = [code for code in codes if code not in KNOWN]
        if unknown:
            return Response({"detail": "That access flag is not on the register."}, status=400)
        ManagerGrant.objects.filter(user=user).delete()
        for code in codes:
            ManagerGrant.objects.create(user=user, capability=code)
        return Response({"id": user.id, "capabilities": codes})

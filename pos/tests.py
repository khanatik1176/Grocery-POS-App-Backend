from datetime import date, timedelta

from django.core import mail
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from pos.models import Product, StockLot

LOC_MEM_MAIL = {
    "default": {"BACKEND": "django.core.mail.backends.locmem.EmailBackend"},
}


@override_settings(MAILERS=LOC_MEM_MAIL, DEBUG=True)
class RegisterTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.admin = self._login("admin@grocerypos.app", "PosAdmin!2026")
        self.manager = None

    def _login(self, email, password):
        response = self.client.post(
            "/api/auth/login/",
            {"email": email, "password": password},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        return response.json()

    def _as(self, session):
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {session['token']}")

    def _receive(self, **overrides):
        payload = {
            "barcode": "8941100000011",
            "name": "Milk",
            "quantity": 10,
            "manufacturing_date": "2026-10-01",
            "expiry_date": "2026-12-01",
            "mrp": "100.00",
            "purchase_rate": "60.00",
            "discount_rate": "0.00",
            "origin": "Agora",
            "location": "Shop",
        }
        payload.update(overrides)
        self._as(self.admin)
        response = self.client.post("/api/inventory/receive/", payload, format="json")
        self.assertEqual(response.status_code, 201, response.content)
        return response.json()

    def test_same_barcode_merges_on_the_list_and_keeps_each_batch(self):
        self._receive()
        self._receive(quantity=2)
        self._receive(
            expiry_date="2027-01-15",
            mrp="110.00",
            purchase_rate="70.00",
            origin="Shwapno",
            quantity=4,
        )
        self._as(self.admin)
        listed = self.client.get("/api/inventory/products/")
        self.assertEqual(listed.status_code, 200)
        rows = listed.json()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["barcode"], "8941100000011")
        self.assertEqual(rows[0]["quantity"], 16)
        self.assertNotIn("id", rows[0])
        detail = self.client.get("/api/inventory/products/8941100000011/")
        lots = detail.json()["lots"]
        self.assertEqual(len(lots), 2)
        self.assertNotIn("id", lots[0])
        self.assertEqual(StockLot.objects.count(), 2)

    def test_combo_marks_products_and_sale_draws_the_products(self):
        self._receive()
        self._receive(
            barcode="8941100000028",
            name="Bread",
            mrp="50.00",
            purchase_rate="30.00",
            quantity=8,
        )
        self._as(self.admin)
        created = self.client.post(
            "/api/inventory/bundles/",
            {
                "name": "Morning set",
                "price": "120.00",
                "items": [
                    {"barcode": "8941100000011", "quantity": 1},
                    {"barcode": "8941100000028", "quantity": 1},
                ],
            },
            format="json",
        )
        self.assertEqual(created.status_code, 201, created.content)
        listed = self.client.get("/api/inventory/products/").json()
        names = {row["name"] for row in listed}
        self.assertEqual(names, {"Milk", "Bread"})
        self.assertEqual(listed[0]["bundle_names"], ["Morning set"])
        sale = self.client.post(
            "/api/sales/checkout/",
            {
                "location": "Shop",
                "lines": [{"bundle": "Morning set", "quantity": 1}],
                "payment": {"method": "cash", "amount_taken": "200.00"},
                "customer_phone": "01711000000",
                "customer_email": "buyer@example.com",
            },
            format="json",
        )
        self.assertEqual(sale.status_code, 201, sale.content)
        body = sale.json()
        self.assertEqual(body["total"], "120.00")
        self.assertEqual(body["change"], "80.00")
        self.assertEqual(body["amount_taken"], "200.00")
        self.assertTrue(body["delivery"]["sms"]["sent"])
        self.assertIn("/api/sales/receipts/", body["receipt_url"])
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("120.00", mail.outbox[0].body)
        milk = StockLot.objects.get(product_id="8941100000011")
        bread = StockLot.objects.get(product_id="8941100000028")
        self.assertEqual(milk.quantity, 9)
        self.assertEqual(bread.quantity, 7)
        self.client.credentials()
        public = self.client.get(f"/api/sales/receipts/{body['number']}/")
        self.assertEqual(public.status_code, 200)
        self.assertIn("Morning set", public.content.decode())

    def test_earliest_expiry_is_sold_first(self):
        self._receive(quantity=1, expiry_date="2026-11-01", purchase_rate="10.00")
        self._receive(quantity=5, expiry_date="2026-12-01", purchase_rate="50.00")
        self._as(self.admin)
        sale = self.client.post(
            "/api/sales/checkout/",
            {
                "location": "Shop",
                "lines": [{"barcode": "8941100000011", "quantity": 2}],
                "payment": {"method": "cash", "amount_taken": "200.00"},
            },
            format="json",
        )
        self.assertEqual(sale.status_code, 201, sale.content)
        early = StockLot.objects.get(expiry_date=date(2026, 11, 1))
        later = StockLot.objects.get(expiry_date=date(2026, 12, 1))
        self.assertEqual(early.quantity, 0)
        self.assertEqual(later.quantity, 4)
        self.assertEqual(sale.json()["total"], "200.00")

    def test_wallet_sms_and_amount_must_match(self):
        self._receive(quantity=1)
        parsed = self.client.post(
            "/api/sales/parse-sms/",
            {
                "text": "You have received Tk 100.00 from 01700000000. Fee Tk 0.00. TrxID 8H45KJ2L at 23/10/2026"
            },
            format="json",
        )
        self.assertEqual(parsed.status_code, 200, parsed.content)
        self.assertEqual(parsed.json()["amount"], "100.00")
        self.assertEqual(parsed.json()["txn_number"], "8H45KJ2L")
        mismatch = self.client.post(
            "/api/sales/checkout/",
            {
                "location": "Shop",
                "lines": [{"barcode": "8941100000011", "quantity": 1}],
                "payment": {"method": "bkash", "amount_taken": "90.00", "txn_number": "8H45KJ2L"},
            },
            format="json",
        )
        self.assertEqual(mismatch.status_code, 400)
        matched = self.client.post(
            "/api/sales/checkout/",
            {
                "location": "Shop",
                "lines": [{"barcode": "8941100000011", "quantity": 1}],
                "payment": {
                    "method": "bkash",
                    "amount_taken": parsed.json()["amount"],
                    "txn_number": parsed.json()["txn_number"],
                },
            },
            format="json",
        )
        self.assertEqual(matched.status_code, 201, matched.content)
        self.assertEqual(matched.json()["change"], "0.00")

    def test_manager_starts_with_nothing_until_each_job_is_assigned(self):
        self.manager = self._login("manager@grocerypos.app", "PosManager!2026")
        self.assertEqual(self.manager["user"]["capabilities"], [])
        self._as(self.manager)
        self.assertEqual(self.client.get("/api/inventory/products/").status_code, 403)
        self.assertEqual(self.client.post("/api/sales/quote/", {}, format="json").status_code, 403)
        self.assertEqual(self.client.get("/api/access/catalog/").status_code, 403)
        self._as(self.admin)
        granted = self.client.put(
            f"/api/access/managers/{self.manager['user']['id']}/",
            {"capabilities": ["inventory.view"]},
            format="json",
        )
        self.assertEqual(granted.status_code, 200, granted.content)
        self._as(self.manager)
        self.assertEqual(self.client.get("/api/inventory/products/").status_code, 200)
        self.assertEqual(self.client.get("/api/summary/?preset=today").status_code, 403)
        me = self.client.get("/api/auth/me/")
        self.assertEqual(me.json()["capabilities"], ["inventory.view"])

    def test_summary_ranges_and_depleted_items_follow_the_threshold(self):
        self._receive(quantity=2)
        self._as(self.admin)
        today = self.client.get("/api/summary/?preset=today")
        self.assertEqual(today.status_code, 200, today.content)
        self.assertEqual(len(today.json()["depleted"]), 1)
        self.assertEqual(today.json()["depleted"][0]["reorder_level"], 5)
        patched = self.client.patch(
            "/api/inventory/products/8941100000011/",
            {"reorder_level": 1},
            format="json",
        )
        self.assertEqual(patched.status_code, 200, patched.content)
        quiet = self.client.get("/api/summary/?preset=today")
        self.assertEqual(quiet.json()["depleted"], [])
        self.client.post(
            "/api/sales/checkout/",
            {
                "location": "Shop",
                "lines": [{"barcode": "8941100000011", "quantity": 1}],
                "payment": {"method": "cash", "amount_taken": "100.00"},
            },
            format="json",
        )
        week = self.client.get("/api/summary/?preset=week")
        self.assertEqual(week.json()["count"], 1)
        self.assertEqual(week.json()["sales_total"], "100.00")
        start = timezone.localdate() - timedelta(days=40)
        custom = self.client.get(
            "/api/summary/",
            {"preset": "range", "start": start.isoformat(), "end": timezone.localdate().isoformat()},
        )
        self.assertEqual(custom.status_code, 200, custom.content)
        self.assertEqual(custom.json()["count"], 1)

    def test_vendor_balance_profit_share_and_lending(self):
        self._receive(quantity=2, purchase_rate="40.00")
        self._as(self.admin)
        paid = self.client.post(
            "/api/accounting/payments/",
            {"vendor": "Agora", "amount": "30.00", "paid_on": "2026-10-23", "note": "Part payment"},
            format="json",
        )
        self.assertEqual(paid.status_code, 201, paid.content)
        self.assertEqual(paid.json()["purchased"], "80.00")
        self.assertEqual(paid.json()["balance"], "50.00")
        partner = self.client.post(
            "/api/accounting/partners/",
            {"name": "Karim", "share": "25.00"},
            format="json",
        )
        self.assertEqual(partner.status_code, 201, partner.content)
        loan = self.client.post(
            "/api/accounting/loans/",
            {
                "borrower": "Rina",
                "amount": "500.00",
                "opened_on": "2026-10-01",
                "due_on": "2026-10-20",
                "note": "Shop advance",
            },
            format="json",
        )
        self.assertEqual(loan.status_code, 201, loan.content)
        self.assertTrue(loan.json()["overdue"])
        collected = self.client.post(
            f"/api/accounting/loans/{loan.json()['id']}/collect/",
            {"amount": "200.00"},
            format="json",
        )
        self.assertEqual(collected.status_code, 200, collected.content)
        self.assertEqual(collected.json()["outstanding"], "300.00")
        overview = self.client.get("/api/accounting/")
        self.assertEqual(overview.status_code, 200, overview.content)
        self.assertEqual(overview.json()["partners"]["shares"][0]["name"], "Karim")

    def test_admin_capabilities_cover_the_register(self):
        self.assertIn("inventory.receive", self.admin["user"]["capabilities"])
        self.assertIn("sales.sell", self.admin["user"]["capabilities"])
        self.assertEqual(len(self.admin["user"]["capabilities"]), 12)
        self._as(self.admin)
        places = self.client.get("/api/inventory/locations/")
        self.assertEqual({row["name"] for row in places.json()}, {"Warehouse", "Shop"})
        self.assertTrue(Product.objects.filter(barcode="8941100000011").count() == 0)

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
        self.assertEqual(len(self.admin["user"]["capabilities"]), 13)
        self._as(self.admin)
        places = self.client.get("/api/inventory/locations/")
        self.assertEqual({row["name"] for row in places.json()}, {"Warehouse", "Shop"})
        self.assertTrue(Product.objects.filter(barcode="8941100000011").count() == 0)

    def test_transfer_moves_batches_without_counting_as_a_purchase(self):
        self._receive(location="Warehouse", quantity=10, purchase_rate="40.00")
        self._as(self.admin)
        moved = self.client.post(
            "/api/inventory/transfer/",
            {
                "barcode": "8941100000011",
                "from_location": "Warehouse",
                "to_location": "Shop",
                "quantity": 4,
            },
            format="json",
        )
        self.assertEqual(moved.status_code, 201, moved.content)
        places = {row["name"]: row["quantity"] for row in moved.json()["locations"]}
        self.assertEqual(places, {"Shop": 4, "Warehouse": 6})
        too_many = self.client.post(
            "/api/inventory/transfer/",
            {
                "barcode": "8941100000011",
                "from_location": "Shop",
                "to_location": "Warehouse",
                "quantity": 5,
            },
            format="json",
        )
        self.assertEqual(too_many.status_code, 400)
        ledger = self.client.get("/api/accounting/vendors/Agora/")
        self.assertEqual(ledger.status_code, 200, ledger.content)
        self.assertEqual(ledger.json()["purchased"], "400.00")
        self.assertEqual(len(ledger.json()["entries"]), 1)

    def test_card_sale_and_summary_breakdowns(self):
        self._receive(quantity=3)
        self._as(self.admin)
        sale = self.client.post(
            "/api/sales/checkout/",
            {
                "location": "Shop",
                "lines": [{"barcode": "8941100000011", "quantity": 2}],
                "payment": {"method": "card", "amount_taken": "200.00"},
            },
            format="json",
        )
        self.assertEqual(sale.status_code, 201, sale.content)
        summary = self.client.get("/api/summary/?preset=week").json()
        self.assertEqual(summary["by_method"][0]["label"], "Card")
        self.assertEqual(summary["top_products"][0]["quantity"], 2)
        self.assertEqual(len(summary["by_day"]), 7)
        self.assertEqual(summary["average"], "200.00")
        accounting = self.client.get("/api/accounting/?preset=month").json()
        self.assertEqual(accounting["period"]["sales_total"], "200.00")
        detail = self.client.get("/api/inventory/products/8941100000011/").json()
        self.assertEqual(detail["last_receipt"]["origin"], "Agora")

    def test_receipt_contacts_are_optional_but_must_be_valid(self):
        self._receive(quantity=5)
        self._as(self.admin)

        def sell(**contact):
            return self.client.post(
                "/api/sales/checkout/",
                {
                    "location": "Shop",
                    "lines": [{"barcode": "8941100000011", "quantity": 1}],
                    "payment": {"method": "cash", "amount_taken": "100.00"},
                    **contact,
                },
                format="json",
            )

        self.assertEqual(sell().status_code, 201)
        self.assertEqual(sell(customer_phone="", customer_email="").status_code, 201)
        bad_phone = sell(customer_phone="017")
        self.assertEqual(bad_phone.status_code, 400)
        self.assertIn("mobile number", bad_phone.json()[0])
        bad_email = sell(customer_email="abc@gmail")
        self.assertEqual(bad_email.status_code, 400)
        self.assertIn("full email address", bad_email.json()["customer_email"][0])
        good = sell(customer_phone="+880 1711-000000")
        self.assertEqual(good.status_code, 201, good.content)
        self.assertEqual(good.json()["delivery"]["sms"]["to"], "01711000000")
        self.assertEqual(StockLot.objects.get().quantity, 2)

    def test_sales_register_pages_filters_and_detail(self):
        self._receive(quantity=40)
        self._as(self.admin)
        numbers = []
        for index in range(27):
            method = "card" if index % 3 == 0 else "cash"
            sale = self.client.post(
                "/api/sales/checkout/",
                {
                    "location": "Shop",
                    "lines": [{"barcode": "8941100000011", "quantity": 1}],
                    "payment": {"method": method, "amount_taken": "100.00"},
                },
                format="json",
            )
            self.assertEqual(sale.status_code, 201, sale.content)
            numbers.append(sale.json()["number"])
        first = self.client.get("/api/sales/history/?preset=today").json()
        self.assertEqual(first["count"], 27)
        self.assertEqual(first["pages"], 2)
        self.assertEqual(len(first["results"]), 25)
        self.assertEqual(first["results"][0]["items"], 1)
        self.assertEqual(first["sales_total"], "2700.00")
        second = self.client.get("/api/sales/history/?preset=today&page=2").json()
        self.assertEqual(len(second["results"]), 2)
        cards = self.client.get("/api/sales/history/?preset=today&method=card").json()
        self.assertEqual(cards["count"], 9)
        found = self.client.get(f"/api/sales/history/?preset=today&q={numbers[4]}").json()
        self.assertEqual([row["number"] for row in found["results"]], [numbers[4]])
        self._receive(barcode="8941100000028", name="Bread", quantity=5)
        mixed = self.client.post(
            "/api/sales/checkout/",
            {
                "location": "Shop",
                "lines": [{"barcode": "8941100000011", "quantity": 2}, {"barcode": "8941100000028", "quantity": 3}],
                "payment": {"method": "cash", "amount_taken": "500.00"},
            },
            format="json",
        )
        self.assertEqual(mixed.status_code, 201, mixed.content)
        by_product = self.client.get("/api/sales/history/?preset=today&q=bread").json()
        self.assertEqual(by_product["count"], 1)
        self.assertEqual(by_product["results"][0]["items"], 5)
        detail = self.client.get(f"/api/sales/history/{numbers[0]}/")
        self.assertEqual(detail.status_code, 200, detail.content)
        self.assertEqual(detail.json()["profit"], "40.00")
        self.assertIn("<html>", detail.json()["receipt_html"])
        self.assertEqual(self.client.get("/api/sales/history/NOPE/").status_code, 404)

    def test_product_list_catalog_and_ledger_pages(self):
        for index in range(23):
            self._receive(
                barcode=f"89411000{index:05d}",
                name=f"Item {index:02d}",
                quantity=2 if index < 4 else 20,
                location="Warehouse" if index % 2 else "Shop",
            )
        self._as(self.admin)
        full = self.client.get("/api/inventory/products/").json()
        self.assertEqual(len(full), 23)
        first = self.client.get("/api/inventory/products/?page=1").json()
        self.assertEqual((first["count"], first["pages"], len(first["results"])), (23, 2, 20))
        self.assertEqual(first["counts"]["low"], 4)
        second = self.client.get("/api/inventory/products/?page=2").json()
        self.assertEqual(len(second["results"]), 3)
        low = self.client.get("/api/inventory/products/?page=1&filter=low").json()
        self.assertEqual(low["count"], 4)
        shop = self.client.get("/api/inventory/products/?page=1&location=Shop").json()
        self.assertEqual(shop["count"], 12)
        catalog = self.client.get("/api/sales/catalog/?q=Item&location=Shop").json()
        self.assertEqual((catalog["page"], catalog["pages"], len(catalog["products"])), (1, 2, 20))
        catalog2 = self.client.get("/api/sales/catalog/?q=Item&location=Shop&page=2").json()
        self.assertEqual((len(catalog2["products"]), catalog2["bundles"]), (3, []))
        ledger = self.client.get("/api/accounting/vendors/Agora/").json()
        self.assertEqual((ledger["count"], ledger["pages"], len(ledger["entries"])), (23, 2, 20))
        ledger2 = self.client.get("/api/accounting/vendors/Agora/?page=2").json()
        self.assertEqual(len(ledger2["entries"]), 3)

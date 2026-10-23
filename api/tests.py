from django.core import mail
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

LOC_MEM_MAIL = {
    "default": {"BACKEND": "django.core.mail.backends.locmem.EmailBackend"},
}


@override_settings(MAILERS=LOC_MEM_MAIL, DEBUG=True)
class AuthAndAdminTests(TestCase):
    def setUp(self):
        self.client = APIClient()

    def test_health_stays_public(self):
        response = self.client.get("/api/health/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")

    def test_admin_can_sign_in_manage_roles_and_sign_out(self):
        login = self.client.post(
            "/api/auth/login/",
            {"email": "admin@grocerypos.app", "password": "PosAdmin!2026"},
            format="json",
        )
        self.assertEqual(login.status_code, 200)
        self.assertEqual(login.json()["user"]["role"]["slug"], "admin")
        token = login.json()["token"]
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {token}")

        created_role = self.client.post("/api/admin/roles/", {"name": "Cashier"}, format="json")
        self.assertEqual(created_role.status_code, 201)
        self.assertEqual(created_role.json()["slug"], "cashier")

        created_user = self.client.post(
            "/api/admin/users/",
            {
                "email": "cashier@grocerypos.app",
                "password": "LaneFour!2026",
                "first_name": "Casey",
                "role_id": created_role.json()["id"],
            },
            format="json",
        )
        self.assertEqual(created_user.status_code, 201)
        self.assertEqual(created_user.json()["role"]["slug"], "cashier")

        logout = self.client.post("/api/auth/logout/")
        self.assertEqual(logout.status_code, 204)
        self.assertEqual(self.client.get("/api/auth/me/").status_code, 401)

    def test_manager_cannot_open_admin_endpoints(self):
        login = self.client.post(
            "/api/auth/login/",
            {"email": "manager@grocerypos.app", "password": "PosManager!2026"},
            format="json",
        )
        self.assertEqual(login.status_code, 200)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {login.json()['token']}")
        self.assertEqual(self.client.get("/api/admin/roles/").status_code, 403)
        self.assertEqual(self.client.get("/api/admin/users/").status_code, 403)

    def test_forgot_password_resets_the_password(self):
        mail.outbox.clear()
        forgot = self.client.post(
            "/api/auth/forgot-password/",
            {"email": "manager@grocerypos.app"},
            format="json",
        )
        self.assertEqual(forgot.status_code, 200)
        self.assertIn("reset_code", forgot.json())
        self.assertEqual(len(mail.outbox), 1)

        reset = self.client.post(
            "/api/auth/reset-password/",
            {
                "email": "manager@grocerypos.app",
                "code": forgot.json()["reset_code"],
                "password": "ShelfCode!2026",
            },
            format="json",
        )
        self.assertEqual(reset.status_code, 200)

        old_login = self.client.post(
            "/api/auth/login/",
            {"email": "manager@grocerypos.app", "password": "PosManager!2026"},
            format="json",
        )
        new_login = self.client.post(
            "/api/auth/login/",
            {"email": "manager@grocerypos.app", "password": "ShelfCode!2026"},
            format="json",
        )
        self.assertEqual(old_login.status_code, 401)
        self.assertEqual(new_login.status_code, 200)

    def test_unknown_email_does_not_reveal_an_account(self):
        response = self.client.post(
            "/api/auth/forgot-password/",
            {"email": "missing@grocerypos.app"},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("reset_code", response.json())

    def test_swagger_schema_is_public(self):
        schema = self.client.get("/api/schema/?format=json")
        docs = self.client.get("/api/docs/")
        self.assertEqual(schema.status_code, 200)
        self.assertIn("/api/auth/login/", schema.json()["paths"])
        self.assertEqual(docs.status_code, 200)

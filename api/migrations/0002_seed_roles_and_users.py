from django.contrib.auth.hashers import make_password
from django.db import migrations


def seed_roles_and_users(apps, schema_editor):
    Role = apps.get_model("api", "Role")
    User = apps.get_model("auth", "User")
    UserProfile = apps.get_model("api", "UserProfile")

    admin_role, _ = Role.objects.get_or_create(slug="admin", defaults={"name": "Admin"})
    manager_role, _ = Role.objects.get_or_create(slug="manager", defaults={"name": "Manager"})

    accounts = [
        {
            "email": "admin@grocerypos.app",
            "password": "PosAdmin!2026",
            "first_name": "Amina",
            "last_name": "Rahman",
            "role": admin_role,
            "is_staff": True,
        },
        {
            "email": "manager@grocerypos.app",
            "password": "PosManager!2026",
            "first_name": "Karim",
            "last_name": "Hossain",
            "role": manager_role,
            "is_staff": False,
        },
    ]
    for account in accounts:
        user = User.objects.filter(username=account["email"]).first()
        if user is None:
            user = User.objects.create(
                username=account["email"],
                email=account["email"],
                password=make_password(account["password"]),
                first_name=account["first_name"],
                last_name=account["last_name"],
                is_staff=account["is_staff"],
                is_active=True,
            )
        UserProfile.objects.get_or_create(user=user, defaults={"role": account["role"]})


def unseed(apps, schema_editor):
    User = apps.get_model("auth", "User")
    Role = apps.get_model("api", "Role")
    User.objects.filter(
        username__in=["admin@grocerypos.app", "manager@grocerypos.app"]
    ).delete()
    Role.objects.filter(slug__in=["admin", "manager"]).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("api", "0001_initial"),
        ("auth", "0012_alter_user_first_name_max_length"),
    ]

    operations = [
        migrations.RunPython(seed_roles_and_users, unseed),
    ]

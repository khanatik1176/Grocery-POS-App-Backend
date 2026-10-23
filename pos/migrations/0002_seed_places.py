from django.db import migrations


def seed(apps, schema_editor):
    Location = apps.get_model("pos", "Location")
    Location.objects.get_or_create(name="Warehouse", defaults={"kind": "warehouse"})
    Location.objects.get_or_create(name="Shop", defaults={"kind": "shop"})
    StoreConfig = apps.get_model("pos", "StoreConfig")
    StoreConfig.objects.get_or_create(pk=1)


def unseed(apps, schema_editor):
    Location = apps.get_model("pos", "Location")
    Location.objects.filter(name__in=["Warehouse", "Shop"]).delete()
    StoreConfig = apps.get_model("pos", "StoreConfig")
    StoreConfig.objects.filter(pk=1).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("pos", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]

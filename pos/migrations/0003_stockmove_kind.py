from django.db import migrations, models


def mark_sales(apps, schema_editor):
    StockMove = apps.get_model("pos", "StockMove")
    StockMove.objects.filter(sale__isnull=False).update(kind="sale")


class Migration(migrations.Migration):
    dependencies = [
        ("pos", "0002_seed_places"),
    ]

    operations = [
        migrations.AddField(
            model_name="stockmove",
            name="kind",
            field=models.CharField(
                choices=[("receive", "Receive"), ("sale", "Sale"), ("transfer", "Transfer")],
                default="receive",
                max_length=16,
            ),
        ),
        migrations.RunPython(mark_sales, migrations.RunPython.noop),
    ]

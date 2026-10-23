CAPABILITIES = (
    ("inventory.view", "Inventory", "View products, quantities, and prices"),
    ("inventory.receive", "Inventory", "Add stock by barcode, QR, or manual entry"),
    ("inventory.locations", "Inventory", "Configure warehouses and shops"),
    ("inventory.bundles", "Inventory", "Create combo bundles"),
    ("inventory.preorder", "Inventory", "Change pre-order alerts"),
    ("sales.sell", "Sales", "Scan, search, and complete sales"),
    ("accounting.view", "Accounting", "View vendor balances and daily sales"),
    ("accounting.pay", "Accounting", "Record vendor payments"),
    ("accounting.profit", "Accounting", "Set profit sharing"),
    ("accounting.lending", "Accounting", "Track lending and collections"),
    ("summary.view", "Summary", "View sales ranges and items to reorder"),
    ("store.settings", "Store", "Configure wallets, receipts, and timeouts"),
)

KNOWN = {code for code, _group, _label in CAPABILITIES}


def capabilities_for(user) -> list[str]:
    if not getattr(user, "is_authenticated", False):
        return []
    profile = getattr(user, "profile", None)
    if profile is None:
        return []
    if profile.role.slug == "admin":
        return [code for code, _group, _label in CAPABILITIES]
    from .models import ManagerGrant

    granted = ManagerGrant.objects.filter(user=user).values_list("capability", flat=True)
    return [code for code in granted if code in KNOWN]


def user_can(user, code: str) -> bool:
    return code in capabilities_for(user)


def catalog() -> list[dict[str, str]]:
    return [
        {"code": code, "group": group, "label": label}
        for code, group, label in CAPABILITIES
    ]

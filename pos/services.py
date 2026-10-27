import base64
import logging
import re
import secrets
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.mail import send_mail
from django.db import transaction
from django.db.models import Count, F, Q, Sum
from django.db.models.functions import Coalesce
from django.utils import timezone
from django.utils.html import escape
from rest_framework.exceptions import NotFound, ValidationError

from .capabilities import user_can
from .models import (
    Bundle,
    BundleItem,
    Loan,
    Location,
    OutboundNotice,
    Product,
    ProfitPartner,
    Sale,
    SaleLine,
    StockLot,
    StockMove,
    StoreConfig,
    Vendor,
    VendorPayment,
)
from .money import bps_to_percent, paisa_to_taka, percent_to_bps, selling_paisa, taka_to_paisa

logger = logging.getLogger(__name__)

AMOUNT_RE = re.compile(r"(?:Tk|BDT|৳)\s*([0-9][0-9,]*(?:\.\d{1,2})?)", re.IGNORECASE)
TXN_RE = re.compile(
    r"(?:TrxID|TxnID|TxnId|Transaction ID|Trx Id)\s*[:#]?\s*([A-Z0-9]+)",
    re.IGNORECASE,
)
MOBILE_RE = re.compile(r"^01[3-9]\d{8}$")
PAYMENT_LABELS = {
    "cash": "Cash",
    "bkash": "bKash",
    "nagad": "Nagad",
    "rocket": "Rocket",
    "card": "Card",
}


def clean_code(value: str) -> str:
    code = (value or "").strip()
    if not code:
        raise ValidationError("Enter a barcode.")
    if len(code) > 64 or any(ord(char) < 32 for char in code):
        raise ValidationError("That code cannot be used.")
    return code


def clean_mobile(value: str) -> str:
    """Accepts 01XXXXXXXXX, +880/880 prefixes, spaces, and dashes; returns 01XXXXXXXXX."""
    digits = re.sub(r"[\s-]", "", value or "")
    if not digits:
        return ""
    if digits.startswith("+880"):
        digits = "0" + digits[4:]
    elif digits.startswith("880"):
        digits = "0" + digits[3:]
    if not MOBILE_RE.match(digits):
        raise ValidationError("Enter a valid mobile number, like 01712345678, or leave it empty.")
    return digits


def clean_name(value: str, label: str, limit: int = 120) -> str:
    name = " ".join((value or "").split())
    if not name:
        raise ValidationError(f"Enter a {label}.")
    if len(name) > limit:
        raise ValidationError(f"That {label} is too long.")
    return name


PAGE_SIZE = 20


def paginate(items, page: int | None, size: int = PAGE_SIZE):
    """Slice a list or queryset to one page; returns (slice, page metadata)."""
    count = items.count() if hasattr(items, "count") and not isinstance(items, list) else len(items)
    pages = max(1, -(-count // size))
    current = min(max(1, page or 1), pages)
    offset = (current - 1) * size
    return items[offset : offset + size], {"count": count, "page": current, "pages": pages}


def _image_path(product: Product) -> str | None:
    if not product.image:
        return None
    return product.image.url


def attach_image(product: Product, encoded: str) -> None:
    if not encoded:
        return
    payload = encoded.strip()
    if payload.startswith("data:") and "," in payload:
        payload = payload.split(",", 1)[1]
    try:
        raw = base64.b64decode(payload, validate=False)
    except Exception as exc:
        raise ValidationError("The photo could not be read.") from exc
    if not raw or len(raw) > 1_500_000:
        raise ValidationError("Use a photo smaller than 1.5 MB.")
    product.image.save(f"{product.barcode}.jpg", ContentFile(raw), save=True)


def effective_preorder(product: Product, config: StoreConfig) -> dict:
    return {
        "reorder_level": product.reorder_level
        if product.reorder_level is not None
        else config.reorder_level,
        "lead_days": product.lead_days if product.lead_days is not None else config.lead_days,
        "notify_enabled": config.notify_enabled
        if product.notify_enabled is None
        else product.notify_enabled,
        "overrides": {
            "reorder_level": product.reorder_level is not None,
            "lead_days": product.lead_days is not None,
            "notify_enabled": product.notify_enabled is not None,
        },
    }


def _lot_payload(lot: StockLot) -> dict:
    unit = selling_paisa(lot.mrp_paisa, lot.discount_bps)
    return {
        "manufacturing_date": lot.manufacturing_date.isoformat(),
        "expiry_date": lot.expiry_date.isoformat(),
        "mrp": paisa_to_taka(lot.mrp_paisa),
        "purchase_rate": paisa_to_taka(lot.purchase_paisa),
        "discount_rate": bps_to_percent(lot.discount_bps),
        "selling_price": paisa_to_taka(unit),
        "origin": lot.origin_id,
        "location": lot.location_id,
        "quantity": lot.quantity,
    }


def _lots_by_product() -> dict[str, list[StockLot]]:
    grouped: dict[str, list[StockLot]] = defaultdict(list)
    lots = StockLot.objects.select_related("origin", "location").order_by(
        "expiry_date", "manufacturing_date", "origin_id"
    )
    for lot in lots:
        grouped[lot.product_id].append(lot)
    return grouped


def _bundle_names() -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = defaultdict(list)
    items = BundleItem.objects.select_related("bundle").order_by("bundle_id")
    for item in items:
        grouped[item.product_id].append(item.bundle_id)
    return grouped


def product_rows(query: str = "") -> list[dict]:
    config = StoreConfig.load()
    products = Product.objects.all()
    needle = query.strip()
    if needle:
        products = products.filter(Q(name__icontains=needle) | Q(barcode__icontains=needle))
    lots = _lots_by_product()
    bundles = _bundle_names()
    today = timezone.localdate()
    rows = []
    for product in products.distinct():
        product_lots = lots.get(product.barcode, [])
        preorder = effective_preorder(product, config)
        quantity = sum(lot.quantity for lot in product_lots)
        locations: dict[str, int] = defaultdict(int)
        selling = []
        mrps = []
        expiries = []
        for lot in product_lots:
            locations[lot.location_id] += lot.quantity
            if lot.quantity:
                selling.append(selling_paisa(lot.mrp_paisa, lot.discount_bps))
                mrps.append(lot.mrp_paisa)
                expiries.append(lot.expiry_date)
        soonest = min(expiries) if expiries else None
        expiring = bool(
            soonest and soonest <= today + timedelta(days=preorder["lead_days"])
        )
        rows.append(
            {
                "barcode": product.barcode,
                "name": product.name,
                "image": _image_path(product),
                "quantity": quantity,
                "locations": [
                    {"name": name, "quantity": qty}
                    for name, qty in sorted(locations.items())
                ],
                "selling_from": paisa_to_taka(min(selling)) if selling else None,
                "selling_to": paisa_to_taka(max(selling)) if selling else None,
                "mrp_from": paisa_to_taka(min(mrps)) if mrps else None,
                "mrp_to": paisa_to_taka(max(mrps)) if mrps else None,
                "soonest_expiry": soonest.isoformat() if soonest else None,
                "low_stock": bool(preorder["notify_enabled"] and quantity <= preorder["reorder_level"]),
                "expiring": expiring,
                "bundle_names": bundles.get(product.barcode, []),
                "preorder": preorder,
            }
        )
    return rows


PRODUCT_FILTERS = {
    "low": lambda row: row["low_stock"],
    "expiring": lambda row: row["expiring"],
    "combo": lambda row: bool(row["bundle_names"]),
}


def product_page(params: dict) -> dict:
    rows = product_rows(params.get("q") or "")
    place = (params.get("location") or "").strip()
    if place:
        rows = [row for row in rows if any(spot["name"] == place and spot["quantity"] > 0 for spot in row["locations"])]
    counts = {"all": len(rows), **{key: sum(1 for row in rows if test(row)) for key, test in PRODUCT_FILTERS.items()}}
    test = PRODUCT_FILTERS.get(params.get("filter") or "")
    if test:
        rows = [row for row in rows if test(row)]
    results, meta = paginate(rows, params.get("page"))
    return {**meta, "counts": counts, "results": results}


def product_detail(barcode: str) -> dict:
    product = Product.objects.filter(pk=clean_code(barcode)).first()
    if product is None:
        raise NotFound("That product is not on the register.")
    rows = product_rows()
    summary = next(row for row in rows if row["barcode"] == product.barcode)
    lots = [
        _lot_payload(lot)
        for lot in StockLot.objects.filter(product=product).order_by(
            "expiry_date", "manufacturing_date", "origin_id", "location_id"
        )
    ]
    summary["lots"] = lots
    last = (
        StockMove.objects.filter(product=product, kind="receive")
        .order_by("-created_at", "-id")
        .first()
    )
    summary["last_receipt"] = (
        {
            "mrp": paisa_to_taka(last.mrp_paisa),
            "purchase_rate": paisa_to_taka(last.purchase_paisa),
            "discount_rate": bps_to_percent(last.discount_bps),
            "origin": last.origin_id,
            "location": last.location_id,
        }
        if last
        else None
    )
    return summary


def receive_stock(user, data: dict) -> dict:
    barcode = clean_code(data["barcode"])
    name = " ".join((data.get("name") or "").split())
    quantity = data["quantity"]
    if quantity < 1 or quantity > 100000:
        raise ValidationError("Enter a quantity between 1 and 100000.")
    if data["expiry_date"] < data["manufacturing_date"]:
        raise ValidationError("Expiry cannot be earlier than the manufacturing date.")
    mrp = taka_to_paisa(data["mrp"])
    purchase = taka_to_paisa(data["purchase_rate"])
    discount = percent_to_bps(data.get("discount_rate", 0))
    origin_name = clean_name(data["origin"], "source of purchase")
    location = Location.objects.filter(pk=clean_name(data["location"], "location", 80)).first()
    if location is None:
        raise ValidationError("Choose a warehouse or shop that already exists.")

    with transaction.atomic():
        product = Product.objects.filter(pk=barcode).first()
        if product is None:
            if not name:
                raise ValidationError("Enter a name for this new product.")
            product = Product.objects.create(barcode=barcode, name=name)
        elif name and name != product.name:
            product.name = name
            product.save(update_fields=["name"])
        attach_image(product, data.get("image_base64") or "")
        vendor, _created = Vendor.objects.get_or_create(name=origin_name)
        lot = StockLot.objects.filter(
            product=product,
            manufacturing_date=data["manufacturing_date"],
            expiry_date=data["expiry_date"],
            mrp_paisa=mrp,
            purchase_paisa=purchase,
            discount_bps=discount,
            origin=vendor,
            location=location,
        ).first()
        if lot is None:
            StockLot.objects.create(
                product=product,
                manufacturing_date=data["manufacturing_date"],
                expiry_date=data["expiry_date"],
                mrp_paisa=mrp,
                purchase_paisa=purchase,
                discount_bps=discount,
                origin=vendor,
                location=location,
                quantity=quantity,
            )
        else:
            lot.quantity += quantity
            lot.save(update_fields=["quantity"])
        StockMove.objects.create(
            kind="receive",
            product=product,
            location=location,
            origin=vendor,
            manufacturing_date=data["manufacturing_date"],
            expiry_date=data["expiry_date"],
            mrp_paisa=mrp,
            purchase_paisa=purchase,
            discount_bps=discount,
            quantity=quantity,
            recorded_by=user,
        )
    return product_detail(barcode)


def transfer_stock(user, data: dict) -> dict:
    """Move stock from one place to another, earliest expiry first.

    Each batch keeps its dates, prices, and source; only the place changes.
    """
    barcode = clean_code(data["barcode"])
    quantity = data["quantity"]
    source = Location.objects.filter(pk=clean_name(data["from_location"], "location", 80)).first()
    target = Location.objects.filter(pk=clean_name(data["to_location"], "location", 80)).first()
    if source is None or target is None:
        raise ValidationError("Choose a warehouse or shop that already exists.")
    if source.pk == target.pk:
        raise ValidationError("Choose two different places.")
    product = Product.objects.filter(pk=barcode).first()
    if product is None:
        raise NotFound("That product is not on the register.")
    with transaction.atomic():
        for lot, taken in _allocate(product, source, quantity, {}):
            lot.quantity -= taken
            lot.save(update_fields=["quantity"])
            identity = {
                "product": product,
                "manufacturing_date": lot.manufacturing_date,
                "expiry_date": lot.expiry_date,
                "mrp_paisa": lot.mrp_paisa,
                "purchase_paisa": lot.purchase_paisa,
                "discount_bps": lot.discount_bps,
                "origin_id": lot.origin_id,
            }
            moved = StockLot.objects.filter(location=target, **identity).first()
            if moved is None:
                StockLot.objects.create(location=target, quantity=taken, **identity)
            else:
                moved.quantity += taken
                moved.save(update_fields=["quantity"])
            for place, signed in ((source, -taken), (target, taken)):
                StockMove.objects.create(
                    kind="transfer",
                    location=place,
                    quantity=signed,
                    recorded_by=user,
                    **identity,
                )
    return product_detail(barcode)


def vendor_names() -> list[dict]:
    return [{"name": vendor.name, "phone": vendor.phone} for vendor in Vendor.objects.all()]


def update_preorder(barcode: str, data: dict) -> dict:
    product = Product.objects.filter(pk=clean_code(barcode)).first()
    if product is None:
        raise NotFound("That product is not on the register.")
    fields = []
    for key in ("reorder_level", "lead_days", "notify_enabled"):
        if key in data:
            setattr(product, key, data[key])
            fields.append(key)
    if fields:
        product.save(update_fields=fields)
    return product_detail(product.barcode)


def location_rows() -> list[dict]:
    return [{"name": location.name, "kind": location.kind} for location in Location.objects.all()]


def create_location(data: dict) -> dict:
    name = clean_name(data["name"], "place", 80)
    kind = data["kind"]
    if kind not in {"warehouse", "shop", "other"}:
        raise ValidationError("Choose warehouse, shop, or other.")
    if Location.objects.filter(pk=name).exists():
        raise ValidationError("That place is already on the register.")
    location = Location.objects.create(name=name, kind=kind)
    return {"name": location.name, "kind": location.kind}


def delete_location(name: str) -> None:
    location = Location.objects.filter(pk=name).first()
    if location is None:
        raise NotFound("That place is not on the register.")
    if location.lots.exists() or location.sales.exists():
        raise ValidationError("This place still has stock or sales, so it stays.")
    location.delete()


def _bundle_payload(bundle: Bundle) -> dict:
    items = [
        {
            "barcode": item.product_id,
            "name": item.product.name,
            "quantity": item.quantity,
        }
        for item in bundle.items.select_related("product")
    ]
    return {
        "name": bundle.name,
        "price": paisa_to_taka(bundle.price_paisa),
        "active": bundle.active,
        "items": items,
    }


def bundle_rows() -> list[dict]:
    return [_bundle_payload(bundle) for bundle in Bundle.objects.all()]


def save_bundle(data: dict, replacing: str | None = None) -> dict:
    name = clean_name(data["name"], "combo name")
    if replacing and name != replacing:
        raise ValidationError("A combo keeps the name it was created with.")
    price = taka_to_paisa(data["price"])
    raw_items = data["items"]
    if len(raw_items) < 2:
        raise ValidationError("A combo needs at least two products.")
    merged: dict[str, int] = {}
    for item in raw_items:
        barcode = clean_code(item["barcode"])
        quantity = item["quantity"]
        if quantity < 1:
            raise ValidationError("Each product in a combo needs a quantity.")
        merged[barcode] = merged.get(barcode, 0) + quantity
    if len(merged) < 2:
        raise ValidationError("A combo needs at least two different products.")
    products = {product.barcode: product for product in Product.objects.filter(pk__in=merged)}
    missing = [barcode for barcode in merged if barcode not in products]
    if missing:
        raise ValidationError("Every product in a combo has to be on the register first.")
    with transaction.atomic():
        if replacing:
            bundle = Bundle.objects.filter(pk=replacing).first()
            if bundle is None:
                raise NotFound("That combo is not on the register.")
            bundle.price_paisa = price
            bundle.active = data.get("active", True)
            bundle.save(update_fields=["price_paisa", "active"])
            bundle.items.all().delete()
        else:
            if Bundle.objects.filter(pk=name).exists():
                raise ValidationError("A combo with this name already exists.")
            bundle = Bundle.objects.create(
                name=name,
                price_paisa=price,
                active=data.get("active", True),
            )
        BundleItem.objects.bulk_create(
            [
                BundleItem(bundle=bundle, product=products[barcode], quantity=quantity)
                for barcode, quantity in merged.items()
            ]
        )
    return _bundle_payload(bundle)


def delete_bundle(name: str) -> None:
    bundle = Bundle.objects.filter(pk=name).first()
    if bundle is None:
        raise NotFound("That combo is not on the register.")
    bundle.delete()


def update_preorder_defaults(data: dict) -> dict:
    config = StoreConfig.load()
    for key in ("reorder_level", "lead_days", "notify_enabled"):
        if key in data and data[key] is not None:
            setattr(config, key, data[key])
    config.save()
    return preorder_defaults()


def preorder_defaults() -> dict:
    config = StoreConfig.load()
    return {
        "reorder_level": config.reorder_level,
        "lead_days": config.lead_days,
        "notify_enabled": config.notify_enabled,
    }


def store_settings() -> dict:
    config = StoreConfig.load()
    return {
        "store_name": config.store_name,
        "bkash_number": config.bkash_number,
        "nagad_number": config.nagad_number,
        "rocket_number": config.rocket_number,
        "payment_timeout_seconds": config.payment_timeout_seconds,
        "sms_enabled": config.sms_enabled,
        "email_enabled": config.email_enabled,
        "sms_template": config.sms_template,
        "receipt_base_url": config.receipt_base_url,
        "sms_mechanism": "log",
        "email_mechanism": "server",
    }


def update_store_settings(data: dict) -> dict:
    config = StoreConfig.load()
    if "store_name" in data:
        config.store_name = clean_name(data["store_name"], "store name", 80)
    for key in ("bkash_number", "nagad_number", "rocket_number"):
        if key in data:
            number = (data[key] or "").strip()
            if len(number) > 20:
                raise ValidationError("Wallet numbers stay within 20 characters.")
            setattr(config, key, number)
    if "payment_timeout_seconds" in data:
        seconds = data["payment_timeout_seconds"]
        if seconds < 15 or seconds > 180:
            raise ValidationError("The payment wait should be between 15 and 180 seconds.")
        config.payment_timeout_seconds = seconds
    if "sms_enabled" in data:
        config.sms_enabled = data["sms_enabled"]
    if "email_enabled" in data:
        config.email_enabled = data["email_enabled"]
    if "sms_template" in data:
        template = (data["sms_template"] or "").strip()
        if not template or len(template) > 240:
            raise ValidationError("Write an SMS template up to 240 characters.")
        config.sms_template = template
    if "receipt_base_url" in data:
        base = (data["receipt_base_url"] or "").strip().rstrip("/")
        if base and not base.startswith(("http://", "https://")):
            raise ValidationError("The receipt link has to start with http:// or https://.")
        config.receipt_base_url = base
    config.save()
    return store_settings()


def payment_config() -> dict:
    config = StoreConfig.load()
    return {
        "store_name": config.store_name,
        "timeout_seconds": config.payment_timeout_seconds,
        "wallets": {
            "bkash": config.bkash_number,
            "nagad": config.nagad_number,
            "rocket": config.rocket_number,
        },
        "sms_enabled": config.sms_enabled,
        "email_enabled": config.email_enabled,
    }


def parse_payment_sms(text: str) -> dict:
    message = (text or "").strip()
    if not message:
        raise ValidationError("Paste the payment SMS first.")
    amount_match = AMOUNT_RE.search(message)
    txn_match = TXN_RE.search(message)
    if not amount_match or not txn_match:
        raise ValidationError("That message does not look like a bKash, Nagad, or Rocket SMS.")
    amount = amount_match.group(1).replace(",", "")
    return {"amount": paisa_to_taka(taka_to_paisa(amount)), "txn_number": txn_match.group(1)}


def _lot_key(lot: StockLot) -> tuple:
    return (
        lot.product_id,
        lot.manufacturing_date,
        lot.expiry_date,
        lot.mrp_paisa,
        lot.purchase_paisa,
        lot.discount_bps,
        lot.origin_id,
        lot.location_id,
    )


@dataclass
class PlannedLine:
    kind: str
    name: str
    barcode: str
    quantity: int
    unit_price_paisa: int
    line_total_paisa: int
    cost_paisa: int
    detail: str = ""


@dataclass
class Plan:
    location: Location
    lines: list[PlannedLine] = field(default_factory=list)
    moves: list[tuple[StockLot, int]] = field(default_factory=list)
    total_paisa: int = 0
    cost_paisa: int = 0


def _allocate(product: Product, location: Location, quantity: int, remaining: dict) -> list[tuple[StockLot, int]]:
    lots = StockLot.objects.filter(
        product=product, location=location, quantity__gt=0
    ).order_by("expiry_date", "manufacturing_date", "origin_id", "mrp_paisa")
    need = quantity
    chosen = []
    for lot in lots:
        key = _lot_key(lot)
        have = remaining.get(key, lot.quantity)
        if have <= 0:
            continue
        take = min(have, need)
        remaining[key] = have - take
        chosen.append((lot, take))
        need -= take
        if need == 0:
            break
    if need:
        available = quantity - need
        raise ValidationError(
            f"Not enough {product.name} at {location.name}. Asked for {quantity}, {available} available."
        )
    return chosen


def build_plan(location_name: str, raw_lines: list[dict]) -> Plan:
    if not raw_lines:
        raise ValidationError("Add at least one item before completing the sale.")
    if len(raw_lines) > 100:
        raise ValidationError("A sale can contain at most 100 lines.")
    location = Location.objects.filter(pk=clean_name(location_name, "location", 80)).first()
    if location is None:
        raise ValidationError("Choose a warehouse or shop that already exists.")

    order: list[tuple[str, str]] = []
    counts: dict[tuple[str, str], int] = {}
    for line in raw_lines:
        quantity = line["quantity"]
        if quantity < 1 or quantity > 10000:
            raise ValidationError("Enter a quantity between 1 and 10000.")
        barcode = (line.get("barcode") or "").strip()
        bundle_name = (line.get("bundle") or "").strip()
        if barcode and bundle_name:
            raise ValidationError("A line is either a product or a combo.")
        if barcode:
            key = ("product", clean_code(barcode))
        elif bundle_name:
            key = ("bundle", clean_name(bundle_name, "combo name"))
        else:
            raise ValidationError("Each line needs a barcode or a combo name.")
        if key not in counts:
            order.append(key)
            counts[key] = 0
        counts[key] += quantity

    plan = Plan(location=location)
    remaining: dict[tuple, int] = {}
    move_map: dict[tuple, list] = {}

    def remember(lot: StockLot, qty: int) -> None:
        key = _lot_key(lot)
        if key not in move_map:
            move_map[key] = [lot, 0]
        move_map[key][1] += qty

    for kind, identity in order:
        quantity = counts[(kind, identity)]
        if kind == "product":
            product = Product.objects.filter(pk=identity).first()
            if product is None:
                raise ValidationError(f"No product is registered for {identity}.")
            for lot, taken in _allocate(product, location, quantity, remaining):
                unit = selling_paisa(lot.mrp_paisa, lot.discount_bps)
                detail = f"Exp {lot.expiry_date:%d %b %Y} · {lot.origin_id}"
                plan.lines.append(
                    PlannedLine(
                        kind="product",
                        name=product.name,
                        barcode=product.barcode,
                        quantity=taken,
                        unit_price_paisa=unit,
                        line_total_paisa=unit * taken,
                        cost_paisa=lot.purchase_paisa * taken,
                        detail=detail,
                    )
                )
                remember(lot, taken)
        else:
            bundle = Bundle.objects.filter(pk=identity, active=True).first()
            if bundle is None:
                raise ValidationError(f"{identity} is not an active combo.")
            items = list(bundle.items.select_related("product"))
            component_bits = []
            cost = 0
            for item in items:
                need = item.quantity * quantity
                component_bits.append(f"{item.product.name} × {need}")
                for lot, taken in _allocate(item.product, location, need, remaining):
                    cost += lot.purchase_paisa * taken
                    remember(lot, taken)
            plan.lines.append(
                PlannedLine(
                    kind="bundle",
                    name=bundle.name,
                    barcode="",
                    quantity=quantity,
                    unit_price_paisa=bundle.price_paisa,
                    line_total_paisa=bundle.price_paisa * quantity,
                    cost_paisa=cost,
                    detail="Includes " + ", ".join(component_bits),
                )
            )

    plan.moves = [(lot, qty) for lot, qty in move_map.values()]
    plan.total_paisa = sum(line.line_total_paisa for line in plan.lines)
    plan.cost_paisa = sum(line.cost_paisa for line in plan.lines)
    return plan


def plan_payload(plan: Plan) -> dict:
    return {
        "location": plan.location.name,
        "total": paisa_to_taka(plan.total_paisa),
        "lines": [
            {
                "kind": line.kind,
                "name": line.name,
                "barcode": line.barcode,
                "quantity": line.quantity,
                "unit_price": paisa_to_taka(line.unit_price_paisa),
                "line_total": paisa_to_taka(line.line_total_paisa),
                "detail": line.detail,
            }
            for line in plan.lines
        ],
    }


def catalog(query: str, location_name: str, page: int = 1) -> dict:
    needle = query.strip()
    location = None
    if location_name:
        location = Location.objects.filter(pk=location_name.strip()).first()
    products = Product.objects.all()
    if needle:
        products = products.filter(Q(name__icontains=needle) | Q(barcode__icontains=needle))
    product_rows_out = []
    product_page_rows, meta = paginate(products.distinct().order_by("name"), page)
    for product in product_page_rows:
        lots = StockLot.objects.filter(product=product, quantity__gt=0)
        if location is not None:
            lots = lots.filter(location=location)
        on_hand = 0
        prices = []
        for lot in lots:
            on_hand += lot.quantity
            prices.append(selling_paisa(lot.mrp_paisa, lot.discount_bps))
        product_rows_out.append(
            {
                "kind": "product",
                "barcode": product.barcode,
                "name": product.name,
                "image": _image_path(product),
                "on_hand": on_hand,
                "price_from": paisa_to_taka(min(prices)) if prices else None,
            }
        )
    bundles = Bundle.objects.filter(active=True)
    if needle:
        bundles = bundles.filter(name__icontains=needle)
    bundle_rows_out = []
    # Combos are few, so they all come with the first page of products.
    for bundle in bundles.order_by("name") if meta["page"] == 1 else []:
        bundle_rows_out.append(
            {
                "kind": "bundle",
                "name": bundle.name,
                "price": paisa_to_taka(bundle.price_paisa),
                "items": [
                    f"{item.product.name} × {item.quantity}"
                    for item in bundle.items.select_related("product")
                ],
            }
        )
    return {**meta, "products": product_rows_out, "bundles": bundle_rows_out}


def _next_number(today: date) -> str:
    prefix = today.strftime("%Y%m%d")
    count = Sale.objects.filter(number__startswith=prefix).count() + 1
    return f"{prefix}-{count:04d}-{secrets.token_hex(2).upper()}"


def _apply_moves(sale: Sale, moves: list[tuple[StockLot, int]], user) -> None:
    for lot, qty in moves:
        fresh = StockLot.objects.filter(
            product_id=lot.product_id,
            manufacturing_date=lot.manufacturing_date,
            expiry_date=lot.expiry_date,
            mrp_paisa=lot.mrp_paisa,
            purchase_paisa=lot.purchase_paisa,
            discount_bps=lot.discount_bps,
            origin_id=lot.origin_id,
            location_id=lot.location_id,
        ).first()
        if fresh is None or fresh.quantity < qty:
            raise ValidationError("Stock changed while the sale was being completed. Try again.")
        fresh.quantity -= qty
        fresh.save(update_fields=["quantity"])
        StockMove.objects.create(
            kind="sale",
            sale=sale,
            product_id=lot.product_id,
            location_id=lot.location_id,
            origin_id=lot.origin_id,
            manufacturing_date=lot.manufacturing_date,
            expiry_date=lot.expiry_date,
            mrp_paisa=lot.mrp_paisa,
            purchase_paisa=lot.purchase_paisa,
            discount_bps=lot.discount_bps,
            quantity=-qty,
            recorded_by=user,
        )


def receipt_url_for(sale: Sale, config: StoreConfig, request=None) -> str:
    path = f"/api/sales/receipts/{sale.number}/"
    base = config.receipt_base_url.strip().rstrip("/")
    if base:
        return f"{base}{path}"
    if request is not None:
        return request.build_absolute_uri(path)
    return path


def receipt_text(sale: Sale) -> str:
    config = StoreConfig.load()
    when = timezone.localtime(sale.created_at).strftime("%d %b %Y %H:%M")
    cashier = ""
    if sale.cashier_id:
        cashier = sale.cashier.get_full_name() or sale.cashier.email
    lines = [config.store_name, f"Receipt {sale.number}", when, sale.location_id]
    if cashier:
        lines.append(f"Cashier {cashier}")
    lines.append("")
    for line in sale.lines.all():
        lines.append(f"{line.name} x{line.quantity}  {paisa_to_taka(line.line_total_paisa)}")
        if line.detail:
            lines.append(f"  {line.detail}")
    lines.append("")
    lines.append(f"Total  {paisa_to_taka(sale.total_paisa)}")
    lines.append(f"{PAYMENT_LABELS.get(sale.payment_method, sale.payment_method)}")
    if sale.payment_method == "cash":
        lines.append(f"Taken  {paisa_to_taka(sale.amount_taken_paisa)}")
        lines.append(f"Change  {paisa_to_taka(sale.change_paisa)}")
    elif sale.txn_number:
        lines.append(f"Txn  {sale.txn_number}")
    return "\n".join(lines)


def receipt_html(sale: Sale) -> str:
    config = StoreConfig.load()
    when = timezone.localtime(sale.created_at).strftime("%d %b %Y %H:%M")
    body = []
    for line in sale.lines.all():
        body.append(
            "<tr>"
            f"<td>{escape(line.name)}</td>"
            f"<td style='text-align:right'>{line.quantity}</td>"
            f"<td style='text-align:right'>{paisa_to_taka(line.line_total_paisa)}</td>"
            "</tr>"
        )
        if line.detail:
            body.append(f"<tr><td colspan='3' style='color:#64748b'>{escape(line.detail)}</td></tr>")
    payment = PAYMENT_LABELS.get(sale.payment_method, sale.payment_method)
    extra = ""
    if sale.payment_method == "cash":
        extra = (
            f"<p>Taken ৳{paisa_to_taka(sale.amount_taken_paisa)}<br>"
            f"Change ৳{paisa_to_taka(sale.change_paisa)}</p>"
        )
    elif sale.txn_number:
        extra = f"<p>Transaction {escape(sale.txn_number)}</p>"
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Receipt {escape(sale.number)}</title>
<style>
body {{ font-family: Georgia, serif; margin: 24px; color: #0f172a; }}
table {{ width: 100%; border-collapse: collapse; }}
td {{ padding: 4px 0; vertical-align: top; }}
</style></head>
<body>
<h1 style="font-size:22px">{escape(config.store_name)}</h1>
<p>Receipt {escape(sale.number)}<br>{escape(when)}<br>{escape(sale.location_id)}</p>
<table>{''.join(body)}</table>
<p><strong>Total ৳{paisa_to_taka(sale.total_paisa)}</strong><br>{escape(payment)}</p>
{extra}
</body></html>"""


def _render_sms(config: StoreConfig, sale: Sale, url: str) -> str:
    values = {
        "url": url,
        "total": paisa_to_taka(sale.total_paisa),
        "number": sale.number,
    }
    try:
        return config.sms_template.format(**values)
    except Exception:
        return f"Thank you for shopping with us. Receipt: {url}"


def deliver_receipt(sale: Sale, request=None) -> dict:
    config = StoreConfig.load()
    url = receipt_url_for(sale, config, request)
    sms = {"sent": False, "detail": "No mobile number was entered."}
    email = {"sent": False, "detail": "No email address was entered."}
    if not sale.customer_phone:
        sms = {"sent": False, "detail": "No mobile number was entered."}
    elif not config.sms_enabled:
        sms = {"sent": False, "detail": "SMS sending is turned off in store settings."}
    else:
        body = _render_sms(config, sale, url)
        OutboundNotice.objects.create(
            sale=sale,
            channel="sms",
            destination=sale.customer_phone,
            body=body,
            mechanism="log",
        )
        logger.warning("SMS via register log to %s: %s", sale.customer_phone, body)
        sms = {"sent": True, "mechanism": "log", "to": sale.customer_phone, "body": body}

    if not sale.customer_email:
        email = {"sent": False, "detail": "No email address was entered."}
    elif not config.email_enabled:
        email = {"sent": False, "detail": "Email sending is turned off in store settings."}
    else:
        body = receipt_text(sale)
        try:
            send_mail(
                f"{config.store_name} receipt {sale.number}",
                body,
                settings.DEFAULT_FROM_EMAIL,
                [sale.customer_email],
            )
        except Exception:
            logger.exception("Receipt email failed for %s", sale.number)
            email = {"sent": False, "detail": "Email could not be sent."}
        else:
            OutboundNotice.objects.create(
                sale=sale,
                channel="email",
                destination=sale.customer_email,
                body=body,
                mechanism="server",
            )
            email = {"sent": True, "mechanism": "server", "to": sale.customer_email}
    return {"sms": sms, "email": email, "receipt_url": url}


def sale_payload(sale: Sale, delivery: dict | None = None) -> dict:
    payload = {
        "number": sale.number,
        "location": sale.location_id,
        "created_at": timezone.localtime(sale.created_at).isoformat(),
        "total": paisa_to_taka(sale.total_paisa),
        "payment_method": sale.payment_method,
        "amount_taken": paisa_to_taka(sale.amount_taken_paisa),
        "change": paisa_to_taka(sale.change_paisa),
        "txn_number": sale.txn_number,
        "lines": [
            {
                "kind": line.kind,
                "name": line.name,
                "barcode": line.barcode,
                "quantity": line.quantity,
                "unit_price": paisa_to_taka(line.unit_price_paisa),
                "line_total": paisa_to_taka(line.line_total_paisa),
                "detail": line.detail,
            }
            for line in sale.lines.all()
        ],
        "receipt_text": receipt_text(sale),
        "receipt_html": receipt_html(sale),
    }
    if delivery is not None:
        payload["delivery"] = delivery
        payload["receipt_url"] = delivery["receipt_url"]
    return payload


def checkout(user, data: dict, request=None) -> dict:
    payment = data["payment"]
    method = payment["method"]
    if method not in PAYMENT_LABELS:
        raise ValidationError("Choose cash, card, bKash, Nagad, or Rocket.")
    customer_phone = clean_mobile(data.get("customer_phone") or "")
    with transaction.atomic():
        plan = build_plan(data["location"], data["lines"])
        taken = taka_to_paisa(payment["amount_taken"])
        txn = (payment.get("txn_number") or "").strip()
        if method == "cash":
            if taken < plan.total_paisa:
                raise ValidationError("Cash taken is less than the total.")
            change = taken - plan.total_paisa
        elif method == "card":
            if taken != plan.total_paisa:
                raise ValidationError("The card amount has to match the sale total.")
            change = 0
        else:
            if not txn:
                raise ValidationError("Enter the transaction number.")
            if taken != plan.total_paisa:
                raise ValidationError("The wallet amount has to match the sale total.")
            change = 0
        number = _next_number(timezone.localdate())
        sale = Sale.objects.create(
            number=number,
            location=plan.location,
            cashier=user if getattr(user, "is_authenticated", False) else None,
            total_paisa=plan.total_paisa,
            cost_paisa=plan.cost_paisa,
            payment_method=method,
            amount_taken_paisa=taken,
            change_paisa=change,
            txn_number=txn,
            customer_phone=customer_phone,
            customer_email=(data.get("customer_email") or "").strip(),
        )
        SaleLine.objects.bulk_create(
            [
                SaleLine(
                    sale=sale,
                    kind=line.kind,
                    name=line.name,
                    barcode=line.barcode,
                    quantity=line.quantity,
                    unit_price_paisa=line.unit_price_paisa,
                    line_total_paisa=line.line_total_paisa,
                    cost_paisa=line.cost_paisa,
                    detail=line.detail,
                )
                for line in plan.lines
            ]
        )
        _apply_moves(sale, plan.moves, user)
    delivery = deliver_receipt(sale, request)
    return sale_payload(sale, delivery)


def _day_bounds(start: date, end: date) -> tuple[datetime, datetime]:
    tz = timezone.get_current_timezone()
    start_dt = timezone.make_aware(datetime.combine(start, time.min), tz)
    end_dt = timezone.make_aware(datetime.combine(end + timedelta(days=1), time.min), tz)
    return start_dt, end_dt


def sales_between(start: date, end: date):
    start_dt, end_dt = _day_bounds(start, end)
    return Sale.objects.filter(created_at__gte=start_dt, created_at__lt=end_dt)


def _sales_block(start: date, end: date) -> dict:
    sales = sales_between(start, end)
    totals = sales.aggregate(
        total=Coalesce(Sum("total_paisa"), 0),
        cost=Coalesce(Sum("cost_paisa"), 0),
    )
    count = sales.count()
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "count": count,
        "sales_total": paisa_to_taka(totals["total"]),
        "profit": paisa_to_taka(totals["total"] - totals["cost"]),
        "average": paisa_to_taka(totals["total"] // count if count else 0),
        "sales": [
            {
                "number": sale.number,
                "created_at": timezone.localtime(sale.created_at).isoformat(),
                "total": paisa_to_taka(sale.total_paisa),
                "payment_method": sale.payment_method,
                "location": sale.location_id,
            }
            for sale in sales.order_by("-created_at")[:50]
        ],
    }


def accounting_overview(user, params: dict | None = None) -> dict:
    today = timezone.localdate()
    payload: dict = {"today": None, "period": None, "vendors": None, "partners": None, "loans": None}
    if user_can(user, "accounting.view") or user_can(user, "accounting.profit"):
        block = _sales_block(today, today)
        payload["today"] = {
            "date": today.isoformat(),
            "count": block["count"],
            "sales_total": block["sales_total"],
            "profit": block["profit"],
        }
        preset, start, end = period_for(params or {})
        period = block if preset == "today" else _sales_block(start, end)
        payload["period"] = {
            "preset": preset,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "count": period["count"],
            "sales_total": period["sales_total"],
            "profit": period["profit"],
        }
    if user_can(user, "accounting.view") or user_can(user, "accounting.pay"):
        payload["vendors"] = vendor_rows()
    if user_can(user, "accounting.profit"):
        profit_paisa = 0
        if payload["period"] is not None:
            profit_paisa = taka_to_paisa(payload["period"]["profit"], allow_negative=True)
        shares = []
        used = 0
        for partner in ProfitPartner.objects.all():
            used += partner.share_bps
            shares.append(
                {
                    "name": partner.name,
                    "share": bps_to_percent(partner.share_bps),
                    "amount": paisa_to_taka((profit_paisa * partner.share_bps) // 10000),
                }
            )
        payload["partners"] = {
            "shares": shares,
            "shop_share": bps_to_percent(10000 - used),
            "shop_amount": paisa_to_taka((profit_paisa * (10000 - used)) // 10000),
        }
    if user_can(user, "accounting.lending"):
        payload["loans"] = loan_rows()
    return payload


def vendor_rows() -> list[dict]:
    purchased = {
        row["origin_id"]: row["total"] or 0
        for row in StockMove.objects.filter(kind="receive", quantity__gt=0)
        .values("origin_id")
        .annotate(total=Sum(F("quantity") * F("purchase_paisa")))
    }
    paid = {
        row["vendor_id"]: row["total"] or 0
        for row in VendorPayment.objects.values("vendor_id").annotate(total=Sum("amount_paisa"))
    }
    rows = []
    for vendor in Vendor.objects.all():
        bought = purchased.get(vendor.name, 0)
        settled = paid.get(vendor.name, 0)
        rows.append(
            {
                "name": vendor.name,
                "phone": vendor.phone,
                "purchased": paisa_to_taka(bought),
                "paid": paisa_to_taka(settled),
                "balance": paisa_to_taka(bought - settled),
            }
        )
    return rows


def vendor_ledger(name: str, page: int = 1) -> dict:
    vendor = Vendor.objects.filter(pk=name).first()
    if vendor is None:
        raise NotFound("That vendor is not on the register.")
    entries = []
    receipts = (
        StockMove.objects.filter(kind="receive", origin=vendor, quantity__gt=0)
        .select_related("product")
        .order_by("created_at", "id")
    )
    for move in receipts:
        entries.append(
            {
                "kind": "purchase",
                "date": timezone.localtime(move.created_at).date().isoformat(),
                "at": move.created_at,
                "title": f"{move.product.name} × {move.quantity}",
                "detail": f"{move.location_id} · Exp {move.expiry_date:%d %b %Y}",
                "paisa": move.purchase_paisa * move.quantity,
            }
        )
    for payment in vendor.payments.order_by("created_at", "id"):
        entries.append(
            {
                "kind": "payment",
                "date": payment.paid_on.isoformat(),
                "at": payment.created_at,
                "title": "Payment",
                "detail": payment.note,
                "paisa": payment.amount_paisa,
            }
        )
    entries.sort(key=lambda entry: (entry["date"], entry["at"]))
    balance = 0
    rows = []
    for entry in entries:
        balance += entry["paisa"] if entry["kind"] == "purchase" else -entry["paisa"]
        rows.append(
            {
                "kind": entry["kind"],
                "date": entry["date"],
                "title": entry["title"],
                "detail": entry["detail"],
                "amount": paisa_to_taka(entry["paisa"]),
                "balance": paisa_to_taka(balance),
            }
        )
    rows.reverse()
    summary = next(row for row in vendor_rows() if row["name"] == vendor.name)
    summary["entries"], meta = paginate(rows, page)
    summary.update(meta)
    return summary


def pay_vendor(user, data: dict) -> dict:
    name = clean_name(data["vendor"], "vendor")
    amount = taka_to_paisa(data["amount"])
    if amount <= 0:
        raise ValidationError("Enter a payment amount.")
    vendor, _created = Vendor.objects.get_or_create(name=name)
    phone = (data.get("phone") or "").strip()
    if phone:
        vendor.phone = phone[:30]
        vendor.save(update_fields=["phone"])
    VendorPayment.objects.create(
        vendor=vendor,
        amount_paisa=amount,
        note=(data.get("note") or "").strip()[:240],
        paid_on=data["paid_on"],
        recorded_by=user if getattr(user, "is_authenticated", False) else None,
    )
    return next(row for row in vendor_rows() if row["name"] == vendor.name)


def save_partner(data: dict) -> dict:
    name = clean_name(data["name"], "partner name")
    bps = percent_to_bps(data["share"])
    others = (
        ProfitPartner.objects.exclude(pk=name).aggregate(total=Coalesce(Sum("share_bps"), 0))["total"]
    )
    if others + bps > 10000:
        raise ValidationError("Profit shares cannot add up to more than 100%.")
    partner, _created = ProfitPartner.objects.update_or_create(
        name=name, defaults={"share_bps": bps}
    )
    return {"name": partner.name, "share": bps_to_percent(partner.share_bps)}


def delete_partner(name: str) -> None:
    partner = ProfitPartner.objects.filter(pk=name).first()
    if partner is None:
        raise NotFound("That partner is not on the register.")
    partner.delete()


def loan_rows() -> list[dict]:
    today = timezone.localdate()
    rows = []
    for loan in Loan.objects.all():
        outstanding = loan.amount_paisa - loan.collected_paisa
        rows.append(
            {
                "id": loan.id,
                "borrower": loan.borrower,
                "amount": paisa_to_taka(loan.amount_paisa),
                "collected": paisa_to_taka(loan.collected_paisa),
                "outstanding": paisa_to_taka(outstanding),
                "opened_on": loan.opened_on.isoformat(),
                "due_on": loan.due_on.isoformat(),
                "note": loan.note,
                "overdue": outstanding > 0 and loan.due_on < today,
            }
        )
    return rows


def create_loan(user, data: dict) -> dict:
    if data["due_on"] < data["opened_on"]:
        raise ValidationError("The due date cannot be earlier than the day it was lent.")
    amount = taka_to_paisa(data["amount"])
    if amount <= 0:
        raise ValidationError("Enter the amount that was lent.")
    loan = Loan.objects.create(
        borrower=clean_name(data["borrower"], "borrower"),
        amount_paisa=amount,
        opened_on=data["opened_on"],
        due_on=data["due_on"],
        note=(data.get("note") or "").strip()[:240],
        recorded_by=user if getattr(user, "is_authenticated", False) else None,
    )
    return next(row for row in loan_rows() if row["id"] == loan.id)


def collect_loan(loan_id: int, data: dict) -> dict:
    loan = Loan.objects.filter(pk=loan_id).first()
    if loan is None:
        raise NotFound("That lending record is not on the register.")
    amount = taka_to_paisa(data["amount"])
    if amount <= 0:
        raise ValidationError("Enter the amount collected.")
    if loan.collected_paisa + amount > loan.amount_paisa:
        raise ValidationError("That collection is more than the amount still outstanding.")
    loan.collected_paisa += amount
    loan.save(update_fields=["collected_paisa"])
    return next(row for row in loan_rows() if row["id"] == loan.id)


def period_for(params: dict) -> tuple[str, date, date]:
    today = timezone.localdate()
    preset = params.get("preset") or "today"
    start = end = today
    if preset == "today":
        start = end = today
    elif preset == "date":
        chosen = params.get("date")
        if not chosen:
            raise ValidationError("Choose a date.")
        start = end = chosen
    elif preset == "week":
        start = today - timedelta(days=today.weekday())
        end = start + timedelta(days=6)
    elif preset in {"week_range", "range"}:
        start = params.get("start")
        end = params.get("end")
        if not start or not end:
            raise ValidationError("Choose a start and an end date.")
        if end < start:
            raise ValidationError("The end date cannot be earlier than the start date.")
    elif preset == "days30":
        start = today - timedelta(days=29)
        end = today
    elif preset == "month":
        start = today.replace(day=1)
        if start.month == 12:
            next_month = start.replace(year=start.year + 1, month=1)
        else:
            next_month = start.replace(month=start.month + 1)
        end = next_month - timedelta(days=1)
    else:
        raise ValidationError("Choose a summary range.")
    return preset, start, end


def _breakdowns(start: date, end: date) -> dict:
    sales = sales_between(start, end)
    by_method = [
        {
            "method": row["payment_method"],
            "label": PAYMENT_LABELS.get(row["payment_method"], row["payment_method"]),
            "count": row["count"],
            "total": paisa_to_taka(row["total"] or 0),
        }
        for row in sales.values("payment_method")
        .annotate(count=Count("number"), total=Sum("total_paisa"))
        .order_by("-total")
    ]
    days: dict[date, list[int]] = {}
    if (end - start).days <= 62:
        cursor = start
        while cursor <= end:
            days[cursor] = [0, 0, 0]
            cursor += timedelta(days=1)
    for sale in sales.only("created_at", "total_paisa", "cost_paisa"):
        day = timezone.localtime(sale.created_at).date()
        bucket = days.setdefault(day, [0, 0, 0])
        bucket[0] += sale.total_paisa
        bucket[1] += sale.total_paisa - sale.cost_paisa
        bucket[2] += 1
    by_day = [
        {
            "date": day.isoformat(),
            "total": paisa_to_taka(values[0]),
            "profit": paisa_to_taka(values[1]),
            "count": values[2],
        }
        for day, values in sorted(days.items())
    ]
    top_products = [
        {
            "name": row["name"],
            "kind": row["kind"],
            "quantity": row["quantity"],
            "total": paisa_to_taka(row["total"] or 0),
        }
        for row in SaleLine.objects.filter(sale__in=sales)
        .values("name", "kind")
        .annotate(quantity=Sum("quantity"), total=Sum("line_total_paisa"))
        .order_by("-total")[:25]
    ]
    return {"by_method": by_method, "by_day": by_day, "top_products": top_products}


HISTORY_PAGE_SIZE = 25


def sale_history(params: dict) -> dict:
    """Every sale in the chosen period, newest first, a page at a time."""
    preset, start, end = period_for(params)
    sales = sales_between(start, end).select_related("cashier")
    method = (params.get("method") or "").strip()
    if method:
        sales = sales.filter(payment_method=method)
    needle = (params.get("q") or "").strip()
    if needle:
        sales = sales.filter(
            Q(number__icontains=needle)
            | Q(txn_number__icontains=needle)
            | Q(customer_phone__icontains=needle)
            | Q(number__in=SaleLine.objects.filter(name__icontains=needle).values("sale_id"))
        )
    totals = sales.aggregate(total=Coalesce(Sum("total_paisa"), 0), cost=Coalesce(Sum("cost_paisa"), 0))
    rows, meta = paginate(
        sales.order_by("-created_at").annotate(items=Coalesce(Sum("lines__quantity"), 0)),
        params.get("page"),
        HISTORY_PAGE_SIZE,
    )
    return {
        "preset": preset,
        "start": start.isoformat(),
        "end": end.isoformat(),
        **meta,
        "sales_total": paisa_to_taka(totals["total"]),
        "profit": paisa_to_taka(totals["total"] - totals["cost"]),
        "results": [
            {
                "number": sale.number,
                "created_at": timezone.localtime(sale.created_at).isoformat(),
                "total": paisa_to_taka(sale.total_paisa),
                "profit": paisa_to_taka(sale.total_paisa - sale.cost_paisa),
                "payment_method": sale.payment_method,
                "location": sale.location_id,
                "items": sale.items,
                "cashier": (sale.cashier.get_full_name() or sale.cashier.email) if sale.cashier_id else "",
                "customer_phone": sale.customer_phone,
            }
            for sale in rows
        ],
    }


def sale_detail(number: str) -> dict:
    sale = Sale.objects.select_related("cashier").prefetch_related("lines", "notices").filter(pk=number).first()
    if sale is None:
        raise NotFound("That sale is not on the register.")
    payload = sale_payload(sale)
    payload.update(
        {
            "cost": paisa_to_taka(sale.cost_paisa),
            "profit": paisa_to_taka(sale.total_paisa - sale.cost_paisa),
            "cashier": (sale.cashier.get_full_name() or sale.cashier.email) if sale.cashier_id else "",
            "customer_phone": sale.customer_phone,
            "customer_email": sale.customer_email,
            "notices": [
                {
                    "channel": notice.channel,
                    "destination": notice.destination,
                    "created_at": timezone.localtime(notice.created_at).isoformat(),
                }
                for notice in sale.notices.all()
            ],
        }
    )
    return payload


def summary_for(params: dict) -> dict:
    preset, start, end = period_for(params)
    block = _sales_block(start, end)
    block["preset"] = preset
    block.update(_breakdowns(start, end))
    rows = product_rows()
    block["expiring"] = [
        {
            "barcode": row["barcode"],
            "name": row["name"],
            "quantity": row["quantity"],
            "soonest_expiry": row["soonest_expiry"],
        }
        for row in rows
        if row["expiring"] and row["quantity"] > 0
    ]
    block["depleted"] = [
        {
            "barcode": row["barcode"],
            "name": row["name"],
            "quantity": row["quantity"],
            "reorder_level": row["preorder"]["reorder_level"],
            "locations": row["locations"],
        }
        for row in rows
        if row["low_stock"]
    ]
    return block

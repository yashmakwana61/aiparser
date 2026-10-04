from __future__ import annotations

import http.client
import time
import xmlrpc.client
from typing import Any
from urllib.parse import urlparse

import structlog

from order_parser.config import get_settings
from order_parser.core import metrics
from order_parser.core.breaker import CircuitOpenError, get_dependency_breaker
from order_parser.models import CustomerModel, ItemModel

logger = structlog.get_logger(__name__)

DEFAULT_UOM_NAMES = {"unit", "units", "nos", "no", "pcs", "pc", "pieces"}

TRANSIENT_ODOO_ERRORS = (OSError, http.client.HTTPException, xmlrpc.client.ProtocolError)

# Test seam: monkeypatched to avoid real sleeping in unit tests.
_sleep = time.sleep


class _TimeoutTransport(xmlrpc.client.Transport):
    """Transport that applies an explicit socket timeout.

    ``xmlrpc.client.ServerProxy`` has no timeout parameter and the stdlib
    default is infinite — a wedged Odoo would hang the pipeline forever.
    Supports plain HTTP and HTTPS (no client certificates).
    """

    def __init__(self, timeout: float, scheme: str) -> None:
        super().__init__()
        self._timeout = timeout
        self._scheme = scheme

    def make_connection(self, host):
        if self._scheme == "https":
            conn = http.client.HTTPSConnection(host, timeout=self._timeout)
        else:
            conn = http.client.HTTPConnection(host, timeout=self._timeout)
        self._connection = (host, conn)
        return conn


class OdooClient:
    """XML-RPC client for the Odoo external API.

    Handles authentication, product catalog lookups, partner resolution,
    unit-of-measure mapping and sales order creation.
    """

    def __init__(
        self,
        url: str | None = None,
        db: str | None = None,
        username: str | None = None,
        password: str | None = None,
        timeout: float | None = None,
        max_attempts: int | None = None,
        backoff_seconds: float | None = None,
    ):
        settings = get_settings()
        self.url = (url or settings.odoo_url).rstrip("/")
        self.db = db or settings.odoo_db
        self.username = username or settings.odoo_user
        self.password = password or settings.odoo_password
        self.timeout = float(timeout if timeout is not None else settings.odoo_timeout_seconds)
        self.max_attempts = max(1, int(max_attempts if max_attempts is not None else settings.odoo_max_attempts))
        self.backoff_seconds = float(
            backoff_seconds if backoff_seconds is not None else settings.odoo_retry_backoff_seconds
        )
        parsed = urlparse(self.url)
        self._scheme = (parsed.scheme or "http").lower()
        self._uid: int | None = None
        self._common: Any = None
        self._models: Any = None

    @property
    def enabled(self) -> bool:
        return bool(self.url and self.db and self.username and self.password)

    def _proxy_common(self):
        if self._common is None:
            self._common = xmlrpc.client.ServerProxy(
                f"{self.url}/xmlrpc/2/common", transport=_TimeoutTransport(self.timeout, self._scheme)
            )
        return self._common

    def _proxy_models(self):
        if self._models is None:
            self._models = xmlrpc.client.ServerProxy(
                f"{self.url}/xmlrpc/2/object", transport=_TimeoutTransport(self.timeout, self._scheme)
            )
        return self._models

    # ------------------------------------------------------------- resilience

    def _call_with_retry(self, operation, label: str) -> Any:
        """Run an XML-RPC call, retrying transient network failures.

        ``xmlrpc.client.Fault`` means Odoo answered with an application-level
        error — deterministic, so it propagates on the first attempt (and is
        not counted as a dependency failure). After exhausting attempts the
        last transient exception is re-raised; the pipeline's outer handler
        already maps that to a structured result. With circuit breakers
        enabled, repeated exhausted calls open the circuit and later calls
        fail fast without touching the network.
        """
        breaker = get_dependency_breaker("odoo", get_settings())
        if breaker is not None and not breaker.allow():
            logger.warning("odoo.circuit_open_fail_fast", call=label)
            raise CircuitOpenError(f"Odoo circuit open; rejected call {label} without network attempt")
        last_exc: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                result = operation()
            except xmlrpc.client.Fault:
                raise
            except TRANSIENT_ODOO_ERRORS as exc:
                last_exc = exc
                metrics.incr("odoo_api_retries_total", outcome="transient")
                logger.warning(
                    "odoo.transient_failure",
                    call=label,
                    attempt=attempt,
                    max_attempts=self.max_attempts,
                    error=str(exc),
                )
                if attempt < self.max_attempts:
                    _sleep(self.backoff_seconds * (attempt - 1))
                continue
            if breaker is not None:
                breaker.record_success()
            return result
        metrics.incr("odoo_api_retries_total", outcome="exhausted")
        if breaker is not None:
            breaker.record_failure()
        assert last_exc is not None
        raise last_exc

    def authenticate(self) -> int:
        if self._uid is None:
            uid = self._call_with_retry(
                lambda: self._proxy_common().authenticate(self.db, self.username, self.password, {}),
                "authenticate",
            )
            if not uid:
                metrics.incr("odoo_auth_failures_total")
                logger.error("odoo.authentication_failed", database=self.db)
                raise ConnectionError(f"Odoo authentication failed for database {self.db}")
            self._uid = int(uid)
        return self._uid

    def execute_kw(self, model: str, method: str, args: list | None = None, kwargs: dict | None = None) -> Any:
        def call():
            return self._proxy_models().execute_kw(
                self.db, self.authenticate(), self.password, model, method, args or [], kwargs or {}
            )

        return self._call_with_retry(call, f"{model}.{method}")

    def fetch_product_catalog(self, limit: int = 5000) -> list[dict[str, Any]]:
        # Read product.product (not product.template) so returned ids are valid
        # for use in sale.order.line product_id. Resolution-layer fields
        # (default_code, uom_id, taxes_id) are additive; servers that reject
        # them fall back to the legacy field set.
        fields = ["id", "name", "list_price", "default_code", "uom_id", "taxes_id"]
        try:
            return self.execute_kw(
                "product.product",
                "search_read",
                [[]],
                {"fields": fields, "limit": limit, "order": "name asc"},
            )
        except xmlrpc.client.Fault:
            logger.warning("odoo.catalog_legacy_fields_fallback")
            return self.execute_kw(
                "product.product",
                "search_read",
                [[]],
                {
                    "fields": ["id", "name", "list_price"],
                    "limit": limit,
                    "order": "name asc",
                },
            )

    def create_product(self, name: str, price: float | None = None) -> int:
        values: dict[str, Any] = {
            "name": name,
            "type": "consu",
            "list_price": float(price) if price is not None else 0.0,
            "sale_ok": True,
        }
        product_id = self.execute_kw("product.product", "create", [values])
        logger.info("odoo.product_created", id=product_id, name=name, price=values["list_price"])
        return product_id

    def find_partner(self, customer: CustomerModel) -> dict[str, Any] | None:
        email = (customer.email or "").strip().lower()
        name = (customer.name or "").strip()
        phone = (customer.phone or "").strip()

        if email:
            found = self.execute_kw(
                "res.partner", "search_read", [[["email", "=ilike", email]]], {"fields": ["id", "name"], "limit": 1}
            )
            if found:
                return found[0]
        if phone:
            found = self.execute_kw(
                "res.partner", "search_read", [[["phone", "=", phone]]], {"fields": ["id", "name"], "limit": 1}
            )
            if found:
                return found[0]
        if name:
            found = self.execute_kw(
                "res.partner", "search_read", [[["name", "=ilike", name]]], {"fields": ["id", "name"], "limit": 1}
            )
            if found:
                return found[0]
        return None

    def create_partner(self, customer: CustomerModel) -> int:
        values: dict[str, Any] = {"name": customer.name or "Unknown Customer"}
        if customer.email:
            values["email"] = customer.email
        if customer.phone:
            values["phone"] = customer.phone
        if customer.address:
            values["street"] = customer.address
        if customer.city:
            values["city"] = customer.city
        if customer.state:
            state_id = self._find_state_id(customer.state)
            if state_id:
                values["state_id"] = state_id
        if customer.zip_code:
            values["zip"] = customer.zip_code
        if customer.gstin:
            values["vat"] = customer.gstin
        if customer.country:
            country_id = self._find_country_id(customer.country)
            if country_id:
                values["country_id"] = country_id
        return self.execute_kw("res.partner", "create", [values])

    def _find_state_id(self, state_name: str) -> int | None:
        """Look up res.country.state by name (e.g. 'Haryana') or code (e.g. '06')."""
        try:
            found = self.execute_kw(
                "res.country.state",
                "search_read",
                [[["|", ["name", "=ilike", state_name], ["code", "=ilike", state_name]]]],
                {"fields": ["id"], "limit": 1},
            )
            return found[0]["id"] if found else None
        except Exception:
            logger.debug("odoo.state_lookup_failed", state=state_name)
            return None

    def _find_country_id(self, country_name: str) -> int | None:
        """Look up res.country by name (e.g. 'India') or code (e.g. 'IN')."""
        try:
            found = self.execute_kw(
                "res.country",
                "search_read",
                [[["|", ["name", "=ilike", country_name], ["code", "=ilike", country_name]]]],
                {"fields": ["id"], "limit": 1},
            )
            return found[0]["id"] if found else None
        except Exception:
            logger.debug("odoo.country_lookup_failed", country=country_name)
            return None

    def find_uom(self, uom_name: str) -> int | None:
        if not uom_name or uom_name.strip().lower() in DEFAULT_UOM_NAMES:
            return None  # Odoo default unit is fine
        found = self.execute_kw(
            "uom.uom", "search_read", [[["name", "=ilike", uom_name]]], {"fields": ["id"], "limit": 1}
        )
        return found[0]["id"] if found else None

    def create_sale_order(self, partner_id: int, items: list[ItemModel], notes: str = "", client_order_ref: str = "") -> dict[str, Any]:
        lines: list[tuple] = []
        for item in items:
            line: dict[str, Any] = {
                "name": item.product_name,
                "product_uom_qty": float(item.quantity or 0),
                "price_unit": float(item.unit_price) if item.unit_price is not None else 0.0,
            }
            if item.product_id:
                line["product_id"] = item.product_id
            uom_id = self.find_uom(item.uom)
            if uom_id:
                line["product_uom"] = uom_id
            # Human-confirmed tax override from the correction flow. When
            # None, Odoo applies product/company defaults (unchanged legacy).
            if item.tax_ids is not None:
                line["tax_id"] = [(6, 0, [int(t) for t in item.tax_ids])]
            lines.append((0, 0, line))

        values: dict[str, Any] = {"partner_id": partner_id, "order_line": lines}
        if notes:
            values["note"] = notes
        if client_order_ref:
            # Short content fingerprint: makes duplicates visible on the
            # Odoo side as well (additive; legacy callers omit it).
            values["client_order_ref"] = client_order_ref

        order_id = self.execute_kw("sale.order", "create", [values])
        try:
            self.execute_kw("sale.order", "action_confirm", [[order_id]])
        except Exception:
            logger.warning("odoo.order_confirmation_failed", order_id=order_id)
        name = self.execute_kw("sale.order", "read", [[order_id], ["name"]])[0]["name"]
        logger.info("odoo.sales_order_created", id=order_id, name=name)
        return {"id": order_id, "name": name}

    def create_telegram_message(
        self,
        chat_id: str,
        sender: str,
        text: str,
        order_id: int,
        state: str = "order_created",
        error_message: str = "",
        raw_update: str = "",
        session_id: str = "",
    ) -> int | None:
        """Best-effort: store one Telegram message linked to a Sale Order."""
        values: dict[str, Any] = {
            "chat_id": str(chat_id),
            "message_text": text or "",
            "state": state,
            "order_id": order_id,
        }
        if sender:
            values["telegram_username"] = sender
        if error_message:
            values["error_message"] = error_message
        if raw_update:
            values["raw_update"] = raw_update
        if session_id:
            values["session_id"] = session_id
        try:
            msg_id = self.execute_kw("telegram.message", "create", [values])
            logger.info("odoo.telegram_message_created", id=msg_id, order_id=order_id)
            return msg_id
        except Exception:
            logger.warning("odoo.telegram_message_create_failed", order_id=order_id, error=True)
            return None

    def upload_file_to_odoo(self, filename: str, data: bytes, res_model: str, res_id: int) -> int | None:
        """Best-effort: upload a file as ir.attachment linked to a record."""
        import base64
        values: dict[str, Any] = {
            "name": filename,
            "res_model": res_model,
            "res_id": res_id,
            "datas": base64.b64encode(data).decode("ascii"),
        }
        try:
            att_id = self.execute_kw("ir.attachment", "create", [values])
            logger.info("odoo.attachment_created", id=att_id, res_model=res_model, res_id=res_id)
            return att_id
        except Exception:
            logger.warning("odoo.attachment_create_failed", res_model=res_model, res_id=res_id)
            return None

    # ------------------------------------------------------------- resolution
    # Read-only helpers for the master data resolution layer. Every method is
    # additive and defensive: failures degrade to None / empty results so the
    # resolution layer can flag an exception instead of guessing.

    def get_partner(self, partner_id: int) -> dict[str, Any] | None:
        found = self.execute_kw(
            "res.partner", "read", [[int(partner_id)], ["name", "email", "phone"]]
        )
        return found[0] if found else None

    def search_partners(self, domain: list, limit: int = 2) -> list[dict[str, Any]]:
        return self.execute_kw(
            "res.partner",
            "search_read",
            [domain],
            {"fields": ["id", "name"], "limit": limit, "order": "id asc"},
        )

    def get_uom(self, uom_id: int) -> dict[str, Any] | None:
        found = self.execute_kw(
            # NOTE: category_id was removed from uom.uom in Odoo 18+.
            "uom.uom", "read", [[int(uom_id)], ["name", "factor"]]
        )
        return found[0] if found else None

    def search_uoms(self, domain: list, limit: int = 1) -> list[dict[str, Any]]:
        return self.execute_kw(
            "uom.uom",
            "search_read",
            [domain],
            {"fields": ["id", "name", "factor"], "limit": limit},
        )

    def list_uoms(self, limit: int = 20) -> list[dict[str, Any]]:
        """Units for human pickers. Degrades to [] — pickers fall back to text entry."""
        try:
            rows = self.execute_kw(
                "uom.uom", "search_read", [[]],
                {"fields": ["id", "name"], "limit": limit, "order": "name asc"},
            )
            return [{"id": r.get("id"), "name": str(r.get("name") or "")} for r in rows or [] if r.get("name")]
        except Exception:
            logger.debug("odoo.list_uoms_failed")
            return []

    def list_sale_taxes(self, limit: int = 20) -> list[dict[str, Any]]:
        """Sale taxes for human pickers. Degrades to [] — pickers fall back gracefully."""
        try:
            rows = self.execute_kw(
                "account.tax", "search_read",
                [[["type_tax_use", "=", "sale"]]],
                {"fields": ["id", "name", "amount"], "limit": limit, "order": "name asc"},
            )
            return [
                {"id": r.get("id"), "name": str(r.get("name") or ""), "amount": r.get("amount")}
                for r in rows or [] if r.get("name")
            ]
        except Exception:
            logger.debug("odoo.list_sale_taxes_failed")
            return []

    def get_partner_pricelist(self, partner_id: int) -> int | None:
        found = self.execute_kw(
            "res.partner", "read", [[int(partner_id)], ["property_product_pricelist"]]
        )
        if not found:
            return None
        pricelist = found[0].get("property_product_pricelist")
        if isinstance(pricelist, (list, tuple)) and pricelist:
            return int(pricelist[0])
        if isinstance(pricelist, int):
            return pricelist
        return None

    def compute_pricelist_price(
        self,
        pricelist_id: int,
        product_id: int,
        quantity: float,
        partner_id: int | None = None,
    ) -> float | None:
        args = [[int(pricelist_id)], [int(product_id)], float(quantity)]
        if partner_id is not None:
            args.append(int(partner_id))
        # Version-tolerant chain: price_get (<=16) then get_products_price (17+).
        try:
            result = self.execute_kw("product.pricelist", "price_get", args)
            if isinstance(result, dict):
                value = result.get(int(product_id), result.get("item"))
                return float(value) if value is not None else None
        except Exception:
            logger.debug("odoo.price_get_unavailable")
        try:
            result = self.execute_kw(
                "product.pricelist", "get_products_price", args
            )
            if isinstance(result, dict):
                value = result.get(int(product_id))
                return float(value) if value is not None else None
            if isinstance(result, list) and result:
                return float(result[0])
        except Exception:
            logger.exception("odoo.pricelist_price_compute_failed")
        return None

    def get_partner_fiscal_position(self, partner_id: int) -> int | None:
        # Version-tolerant chain: Odoo 18 renamed the field to
        # property_account_position_id; older versions use
        # property_account_position. Either may be absent — a missing
        # fiscal position is not an error, taxes simply stay unmapped.
        for field in ("property_account_position_id", "property_account_position"):
            try:
                found = self.execute_kw(
                    "res.partner", "read", [[int(partner_id)], [field]]
                )
            except Exception:
                logger.debug("odoo.fiscal_position_field_unavailable", field=field)
                continue
            if not found:
                return None
            position = found[0].get(field)
            if isinstance(position, (list, tuple)) and position:
                return int(position[0])
            if isinstance(position, int):
                return position
            return None
        return None

    def map_taxes_through_fiscal_position(
        self, fiscal_position_id: int, tax_ids: list[int]
    ) -> list[int]:
        rows = self.execute_kw(
            "fiscal.position.tax",
            "search_read",
            [[["position_id", "=", int(fiscal_position_id)]]],
            {"fields": ["tax_src_id", "tax_dest_id"]},
        )
        mapping = {}
        for row in rows or []:
            src = row.get("tax_src_id")
            dest = row.get("tax_dest_id")
            if isinstance(src, (list, tuple)):
                src = src[0] if src else None
            if isinstance(dest, (list, tuple)):
                dest = dest[0] if dest else None
            if src is not None:
                mapping[int(src)] = dest

        mapped: list[int] = []
        for tax_id in tax_ids or []:
            dest = mapping.get(int(tax_id), tax_id)
            if dest is not None and int(dest) not in mapped:
                mapped.append(int(dest))
        return mapped

    def default_sale_taxes(self) -> list[int]:
        try:
            value = self.execute_kw(
                "ir.default", "get", ["product.template", "taxes_id"]
            )
        except Exception as exc:
            # Newer Odoo versions removed ir.default.get. Missing company
            # default taxes is not fatal — product/customer taxes apply —
            # so stay quiet and let callers fall back to [].
            logger.debug("odoo.default_taxes_lookup_unavailable", error=str(exc))
            return []
        if isinstance(value, (list, tuple)):
            return [int(t) for t in value if t]
        return []
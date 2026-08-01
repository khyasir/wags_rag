"""Odoo RPC client. Authenticate once, reuse the uid.

**Transport is JSON-RPC, not XML-RPC.** The task specified XML-RPC and that was
the starting point, but Odoo 16 hardcodes ``allow_none=False`` when marshalling
XML-RPC responses (``odoo/addons/base/controllers/rpc.py``, ``_xmlrpc``). A
``read_group`` sum over a column that is NULL in every matched row comes back as
None, and the server then fails to marshal its own reply:

    TypeError: cannot marshal None unless allow_none is enabled

That is not hypothetical — the ``discounts`` metric hits it immediately, because
``coupon_amount`` and ``voucher_amount`` are NULL on all 318 local orders. It is
a server-side limit, so no client setting fixes it.

``/jsonrpc`` is Odoo's own equally-supported endpoint, same ``execute_kw``, same
authentication, and it carries null natively. Verified against the live database
on the exact call that failed. Everything else in the spec is unchanged.

Lookups are read with ``active_test=False`` — see ``load_lookups``.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Optional

from catalog import LookupRecord, Lookups
from config import Settings, settings

LANGS = ("en_US", "ar_001")

#: model -> (cache attribute, flag fields to read)
LOOKUP_MODELS = {
    "branch.wags": ("branches", ()),
    "order.type.wags": ("order_types", ("is_return", "is_aggregator")),
    "pos.payment.method.wags": ("payment_methods", ("is_cash", "is_span",
                                                    "is_aggregator")),
    "product.category.wags": ("categories", ()),
}


class RpcError(RuntimeError):
    """Odoo call failed. Never swallowed — rule 9 forbids inventing a number."""


@dataclass
class Odoo:
    cfg: Settings
    uid: Optional[int] = None

    # ------------------------------------------------------------- transport

    def _call(self, service: str, method: str, args: list):
        payload = json.dumps({
            "jsonrpc": "2.0",
            "method": "call",
            "params": {"service": service, "method": method, "args": args},
            "id": 1,
        }).encode()
        req = urllib.request.Request(
            f"{self.cfg.odoo_url.rstrip('/')}/jsonrpc", data=payload,
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.cfg.rpc_timeout) as resp:
                body = json.loads(resp.read())
        except urllib.error.URLError as exc:
            raise RpcError(
                f"Could not reach Odoo at {self.cfg.odoo_url}: {exc.reason}") from exc
        except (OSError, ValueError) as exc:
            raise RpcError(f"Odoo call {service}.{method} failed: {exc}") from exc

        if "error" in body:
            raise RpcError(_clean_error(body["error"]))
        return body.get("result")

    def connect(self) -> "Odoo":
        self.uid = self._call("common", "login",
                              [self.cfg.odoo_db, self.cfg.odoo_user,
                               self.cfg.odoo_key])
        if not self.uid:
            raise RpcError(
                f"Odoo rejected the login for user '{self.cfg.odoo_user}' on "
                f"database '{self.cfg.odoo_db}'.")
        return self

    # ----------------------------------------------------------------- calls

    def execute_kw(self, model: str, method: str, args: list,
                   kwargs: Optional[dict] = None):
        if self.uid is None:
            self.connect()
        return self._call("object", "execute_kw",
                          [self.cfg.odoo_db, self.uid, self.cfg.odoo_key,
                           model, method, args, kwargs or {}])

    def read_group(self, model: str, domain: list, fields: list, groupby: list,
                   context: Optional[dict] = None) -> tuple:
        """Returns (rows, elapsed_ms). ``lazy=False`` so every grouping applies."""
        started = time.time()
        rows = self.execute_kw(
            model, "read_group", [_plain(domain), fields, groupby],
            {"lazy": False, "context": context or {}})
        return rows, (time.time() - started) * 1000

    def search_count(self, model: str, domain: list) -> int:
        return self.execute_kw(model, "search_count", [_plain(domain)])

    # --------------------------------------------------------------- lookups

    def load_lookups(self) -> Lookups:
        """Read the reference models once. Small tables, safe to read whole.

        ``active_test=False`` is essential, not a nicety. In the live database
        the only order type flagged ``is_return`` (id 2, "Return") is archived,
        and so is the only ``is_cash`` payment method (id 9). Odoo hides archived
        records by default, so without this the cache would report zero return
        types — and then return orders would be counted into gross sales in
        silence, breaking the one rule that says never report net alone.

        Historical orders keep pointing at archived types, so the cache must
        contain archived rows to interpret them.
        """
        caches = {}
        for model, (attr, flags) in LOOKUP_MODELS.items():
            caches[attr] = tuple(self._load_one(model, flags))
        return Lookups(**caches)

    def _load_one(self, model: str, flag_fields: tuple) -> list:
        fields = ["name", *flag_fields]
        by_lang = {}
        for lang in LANGS:
            try:
                by_lang[lang] = self.execute_kw(
                    model, "search_read", [[], fields],
                    {"context": {"lang": lang, "active_test": False}})
            except RpcError:
                # A language that is not installed is not an error.
                continue

        if not by_lang:
            raise RpcError(f"Could not read the lookup model {model}.")

        primary = by_lang.get(LANGS[0]) or next(iter(by_lang.values()))
        names_by_id: dict = {}
        for rows in by_lang.values():
            for r in rows:
                names_by_id.setdefault(r["id"], set()).add(str(r.get("name") or ""))

        out = []
        for r in primary:
            variants = {n.strip().lower() for n in names_by_id.get(r["id"], ()) if n}
            out.append(LookupRecord(
                id=r["id"],
                name=str(r.get("name") or f"#{r['id']}"),
                search_names=tuple(sorted(variants)),
                # NULL flags read as False, never as unknown.
                flags={f: bool(r.get(f)) for f in flag_fields},
            ))
        return out

    def resolve_product_ids(self, keyword: str, limit: int = 200) -> list:
        """Product ids for a keyword.

        ``product.product`` is too large to cache, so this is a live name_search
        against the product table — never against the 26-million-row line table.
        Only ids ever reach the line query.
        """
        pairs = self.execute_kw("product.product", "name_search", [],
                                {"name": keyword, "limit": limit})
        return [p[0] for p in pairs]


def _plain(domain: list) -> list:
    """Odoo accepts lists over JSON; tuples would serialise the same but be
    inconsistent to read in logs."""
    return [list(t) if isinstance(t, tuple) else t for t in domain]


def _clean_error(err: dict) -> str:
    """The useful part of an Odoo error payload."""
    data = err.get("data") or {}
    msg = (data.get("message") or "").strip()
    name = (data.get("name") or "").split(".")[-1]
    if msg:
        return f"Odoo error: {msg.splitlines()[-1].strip()}"
    return f"Odoo error: {name or err.get('message') or 'unknown failure'}"


def connect(cfg: Optional[Settings] = None) -> Odoo:
    return Odoo(cfg or settings()).connect()

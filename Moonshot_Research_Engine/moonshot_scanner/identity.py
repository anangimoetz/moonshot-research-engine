"""Curated project identity and contract-scoped DEX evidence.

Never infer a token contract from its ticker, search ranking, logo, or liquidity.
The registry is intentionally small: unreviewed projects remain UNKNOWN while
their exchange tape can still be discovered. Pool liquidity is not executable
exit liquidity and cannot establish LOW liquidity risk by itself.
"""
from __future__ import annotations

import copy
import datetime as dt
import json
import math
from pathlib import Path
import re
import time

REGISTRY_PATH = Path(__file__).with_name("asset_registry.json")
_EVM = {"ethereum", "bsc", "arbitrum", "optimism", "base", "polygon", "avalanche", "fantom", "linea", "mantle", "zksync", "scroll", "cronos", "celo", "blast", "sonic", "berachain"}
_STABLES = {"USDT", "USDC", "DAI", "USDE", "FDUSD", "TUSD", "USDS"}


def _num(value):
    if isinstance(value,bool):
        return None
    try:
        n = float(value)
        return n if math.isfinite(n) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _utc(ms):
    return dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def normalize_contract(chain, address):
    """EVM hex is case-insensitive; Solana/TON/base58 case is significant."""
    chain = str(chain or "").lower().strip()
    address = str(address or "").strip()
    if chain in _EVM and re.fullmatch(r"0x[0-9a-fA-F]{40}", address):
        return address.lower()
    return address


def get_identity(symbol):
    """Return a fresh dictionary, never a mutable global registry entry.

    A verified project may have no approved DEX representations. That means
    on-chain coverage is unknown, not that the project is illiquid or unsafe.
    """
    base = str(symbol).upper().replace("/", "").replace("-", "").strip()
    if base.endswith("USDT"):
        base = base[:-4]
    registry = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    item = registry.get("assets", {}).get(base)
    if item is None:
        return {"symbol": base, "status": "UNKNOWN", "coingecko_id": None,
                "contracts": [], "official_sources": [], "reviewed_at": None,
                "reason": "Project ID and token contracts have not been reviewed"}
    item = copy.deepcopy(item)
    item.update({"symbol": base, "status": "VERIFIED_PROJECT", "reviewed_at": registry.get("reviewed_at")})
    return item


def registry_ids():
    """Curated ID set for a single rate-efficient CoinGecko markets request."""
    registry = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    return {sym: row["coingecko_id"] for sym, row in registry.get("assets", {}).items() if row.get("coingecko_id")}


def aggregate_dex_pairs(identity, pairs, cex_price=None, now_ms=None):
    """Validate, deduplicate and aggregate only approved base-token pools.

    A pool is rejected on an unverified base or quote contract, duplicate chain/pair address,
    >15% CEX price divergence, invalid liquidity/price, or an implausibly small
    quoted stable reserve relative to advertised liquidity. Such anomalies are
    data-quality findings, not claims that a token/pool is fraudulent.
    """
    now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    approved = {(str(c.get("chain", "")).lower(), normalize_contract(c.get("chain"), c.get("address")))
                for c in identity.get("contracts", []) if c.get("address") and c.get("source")}
    registry = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    quotes = {(str(c.get("chain", "")).lower(), normalize_contract(c.get("chain"), c.get("address"))): c
              for c in registry.get("quote_contracts", []) if c.get("address") and c.get("source")}
    for symbol, asset in registry.get("assets", {}).items():
        for c in asset.get("contracts", []):
            if c.get("address") and c.get("source"):
                quotes[(str(c.get("chain", "")).lower(), normalize_contract(c.get("chain"), c.get("address")))] = dict(c, symbol=symbol)
    answer = {"status": "UNKNOWN", "identity_status": identity.get("status", "UNKNOWN"),
              "dex_liquidity_usd": None, "executable_liquidity_usd": None,
              "liquidity_risk": "UNKNOWN", "dex_volume_24h_usd": None,
              "buy_transaction_share_1h": None, "pools": [], "rejected": [],
              "observed_at": None, "fetched_at": None, "fetched_at_ms": None, "evaluated_at": _utc(now_ms),
              "coverage_note": "Only explicitly approved contracts and returned pools are covered; no claim of complete cross-chain liquidity"}
    if not approved or identity.get("status") != "VERIFIED_PROJECT":
        answer["reason"] = "No independently verified contract mapping; ticker search cannot supply one"
        return answer
    seen = set()
    buy_count = sell_count = 0
    reference = _num(cex_price)
    for pair in pairs or []:
        if not isinstance(pair, dict):
            answer["rejected"].append({"reason": "MALFORMED_POOL"})
            continue
        chain = str(pair.get("chainId", "")).lower()
        address = (pair.get("baseToken") or {}).get("address")
        pair_address = pair.get("pairAddress")
        def reject(reason):
            answer["rejected"].append({"chain": chain, "pair_address": pair_address,
                                       "token_address": address, "reason": reason})
        if (chain, normalize_contract(chain, address)) not in approved:
            reject("UNVERIFIED_TOKEN_CONTRACT")
            continue
        quote_contract = (pair.get("quoteToken") or {}).get("address")
        quote_identity = quotes.get((chain, normalize_contract(chain, quote_contract)))
        if quote_identity is None:
            reject("UNVERIFIED_QUOTE_CONTRACT")
            continue
        if not pair_address:
            reject("PAIR_ADDRESS_MISSING")
            continue
        key = (chain, normalize_contract(chain, pair_address))
        if key in seen:
            reject("DUPLICATE_POOL")
            continue
        seen.add(key)
        price = _num(pair.get("priceUsd"))
        liquidity = pair.get("liquidity") or {}
        total = _num(liquidity.get("usd"))
        if price is None or price <= 0 or total is None or total <= 0:
            reject("PRICE_OR_LIQUIDITY_MISSING")
            continue
        divergence = abs(price / reference - 1) * 100 if reference and reference > 0 else None
        if divergence is not None and divergence > 15:
            reject("PRICE_DIVERGENCE_OVER_15_PCT")
            continue
        quote_symbol = str(quote_identity.get("symbol", "")).upper()
        quote_reserve = _num(liquidity.get("quote"))
        if quote_symbol in _STABLES and (quote_reserve is None or quote_reserve < max(10, total * .005)):
            reject("QUOTED_STABLE_RESERVE_TOO_SMALL_OR_UNKNOWN")
            continue
        tx = (pair.get("txns") or {}).get("h1") or {}
        buys, sells = _num(tx.get("buys")), _num(tx.get("sells"))
        if buys is not None and sells is not None and buys >= 0 and sells >= 0:
            buy_count += buys
            sell_count += sells
        volume = _num((pair.get("volume") or {}).get("h24"))
        volume = volume if volume is not None and volume >= 0 else None
        created = _num(pair.get("pairCreatedAt"))
        answer["pools"].append({"chain": chain, "pair_address": pair_address, "token_address": address,
                                "dex": pair.get("dexId"), "url": pair.get("url"),
                                "price_usd": price, "price_divergence_pct": divergence,
                                "snapshot_liquidity_usd": total, "volume_24h_usd": volume,
                                "quote_token": pair.get("quoteToken"), "quote_reserve_units": quote_reserve,
                                "pair_created_at_ms": int(created) if created is not None else None,
                                "observed_at_ms": None, "fetched_at_ms": pair.get("_source_fetched_at_ms"),
                                "quote_token_identity_status": "VERIFIED_CONTRACT", "quote_identity_source": quote_identity.get("source")})
    accepted = answer["pools"]
    if accepted:
        volumes = [p["volume_24h_usd"] for p in accepted if p["volume_24h_usd"] is not None]
        fetched = max((p["fetched_at_ms"] for p in accepted if p["fetched_at_ms"] is not None), default=None)
        answer.update({"status": "VERIFIED_POOLS", "dex_liquidity_usd": sum(p["snapshot_liquidity_usd"] for p in accepted),
                       "dex_volume_24h_usd": sum(volumes) if volumes else None,
                       "fetched_at_ms": fetched, "fetched_at": _utc(fetched) if fetched is not None else None,
                       "buy_transaction_share_1h": buy_count / (buy_count + sell_count) if buy_count + sell_count else None,
                       "reason": "Pool identities verified; executable exit depth and observation time remain unverified"})
    else:
        answer["reason"] = "No returned pool passed identity and data-quality checks"
    return answer

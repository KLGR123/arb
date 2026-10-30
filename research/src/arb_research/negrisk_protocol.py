"""Scanner qualification over ARB-27's shared selected-asset view.

This module adds strategy support rules, never another asset-ID parser.
"""

from .polymarket.assets import require_assets, select_assets


def identity(market, event_id="", observed_at=None):
    return select_assets(market, event_id=event_id, observed_at=observed_at)


def qualified_assets(market, view=None):
    view = identity(market) if view is None else view
    selected = require_assets(view)
    by_outcome = {a.outcome.strip().casefold(): a for a in selected}
    if set(by_outcome) != {"yes", "no"}:
        raise ValueError("expected_binary_yes_no_assets")
    module = market.get("module")
    # Address-to-module verification is outside this read-only scanner. A name
    # cannot override a contradictory or unqualified address from the source.
    if market.get("moduleAddress") is not None:
        raise ValueError("unverified_module_address")
    # CTF has no PositionManager module metadata. An explicit contradictory
    # module is rejected; a V2 market must carry positive module evidence.
    if view.protocol_version == "v1":
        if module not in (None, "v1_ctf"):
            raise ValueError("unsupported_ctf_module")
    elif module != "neg_risk":
        raise ValueError("unsupported_combo_module" if module == "combo" else
                         "unsupported_or_missing_negrisk_module")
    group_id = market.get("negRiskMarketID")
    if (market.get("negRisk") is not True or not isinstance(group_id, str)
            or not group_id.strip()):
        raise ValueError("unverified_negrisk_group_membership")
    if (market.get("closed") or market.get("active") is not True
            or market.get("acceptingOrders") is not True
            or market.get("enableOrderBook") is not True):
        raise ValueError("inactive/closed/non-accepting market")
    return by_outcome


def asset_wire(asset):
    result = asset.model_dump(mode="json")
    if asset.protocol_version == "v1" and result["module"] is None:
        result["module"] = "v1_ctf"
    return result


def diagnostic(market, view, reason, **context):
    return dict(context, market_id=str(market.get("id", "")), reason=reason,
                version=market.get("version"), protocol_version=view.protocol_version,
                asset_kind=("ctf_token" if view.protocol_version == "v1" else
                            "position" if view.protocol_version == "v2" else None),
                module=market.get("module") or market.get("moduleAddress"),
                assets=[asset_wire(a) for a in view.assets],
                raw_identity=view.raw_identity)


def group_reasons(event):
    """Check only identity/structure; book and fee failures are checked later."""
    reasons, identities = [], []
    markets = event.get("markets", [])
    if not isinstance(markets, list) or len(markets) < 2:
        return ["incomplete_negrisk_group"]
    seen_markets, seen_conditions, seen_assets = set(), set(), set()
    for market in markets:
        view = identity(market)
        try:
            selected = qualified_assets(market, view)
            mid = str(market.get("id", ""))
            condition = selected["yes"].condition_id
            # Compare canonical numeric identity without changing the wire ID.
            asset_keys = {(a.asset_id.lstrip("0") or "0") for a in selected.values()}
            if (not mid or mid in seen_markets or condition in seen_conditions
                    or seen_assets.intersection(asset_keys)):
                raise ValueError("duplicate_market_condition_or_asset")
            seen_markets.add(mid)
            seen_conditions.add(condition)
            seen_assets.update(asset_keys)
            identities.append((view.protocol_version, selected["yes"].ledger,
                               selected["yes"].chain_id, market["negRiskMarketID"]))
        except (ValueError, KeyError, TypeError):
            reasons.append("incomplete_negrisk_group")
    if len(set(identities)) > 1:
        reasons.append("mixed_protocol_or_negrisk_group")
    if identities and event.get("negRiskMarketID") and any(
            row[-1] != event["negRiskMarketID"] for row in identities):
        reasons.append("event_negrisk_group_mismatch")
    for flag, reason in (("negRiskAugmented", "augmented_negrisk"),):
        if event.get(flag):
            reasons.append(reason)
    if any(m.get("negRiskOther") for m in markets):
        reasons.append("other_outcome")
    if any(m.get("negRiskPlaceholder") for m in markets):
        reasons.append("placeholder_outcome")
    return sorted(set(reasons))

"""Shared Gamma identity boundary. Never coerce an asset ID through a number.

Both ID families may be retained for provenance; only an explicit version can
select one. Invalid identity quarantines the whole binary market atomically.
"""

import json
import re
from importlib.metadata import version

from .models import AssetSelection, SelectedAsset, Token

SDK_VERSION = version("polymarket-client")
IDENTITY_FIELDS = ("version", "clobTokenIds", "positionIds", "outcomes", "conditionId",
                   "module", "moduleAddress", "chainId")


def array(value):
    try:
        value = json.loads(value) if isinstance(value, str) else value
    except (ValueError, TypeError):
        raise ValueError("invalid_identity_array") from None
    if not isinstance(value, list):
        raise ValueError("missing_identity_array")
    return value


def decimal_id(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9]+", value) is not None


def historical_tokens(raw):
    """Compatibility metadata only; never a consumer's selection input."""
    result = []
    try:
        labels = array(raw.get("outcomes"))
        for protocol, key in (("v1", "clobTokenIds"), ("v2", "positionIds")):
            try:
                ids = array(raw.get(key))
            except ValueError:
                continue
            for i, (label, asset_id) in enumerate(zip(labels, ids)):
                if isinstance(label, str) and label.strip() and decimal_id(asset_id):
                    result.append(Token(id=asset_id, outcome=label, outcome_index=i, protocol=protocol))
    except ValueError:
        pass
    return result


def select_assets(raw: dict, *, event_id: str = "", observed_at: str | None = None,
                  received_at: str | None = None) -> AssetSelection:
    protocol = raw.get("version")
    protocol = protocol if isinstance(protocol, str) and protocol in ("v1", "v2") else "unknown"
    view = AssetSelection(protocol_version=protocol,
                          raw_identity={k: raw[k] for k in IDENTITY_FIELDS if k in raw})
    try:
        if protocol == "unknown":
            raise ValueError("missing_protocol_version" if raw.get("version") is None else "unknown_protocol_version")
        condition = raw.get("conditionId")
        if not isinstance(condition, str) or not condition.strip():
            raise ValueError("missing_condition_id")
        labels = array(raw.get("outcomes"))
        if (len(labels) != 2 or any(not isinstance(x, str) or not x.strip() for x in labels)
                or len({x.strip().casefold() for x in labels}) != 2):
            raise ValueError("invalid_binary_outcomes")
        ids = array(raw.get("clobTokenIds" if protocol == "v1" else "positionIds"))
        if len(ids) != len(labels):
            raise ValueError("outcome_asset_count_mismatch")
        if not all(decimal_id(x) for x in ids):
            raise ValueError("invalid_decimal_asset_id_string")
        if len({x.lstrip("0") or "0" for x in ids}) != len(ids):
            raise ValueError("duplicate_asset_id")
        module = raw.get("module") or raw.get("moduleAddress")
        if type(module) is int:
            module = str(module)
        if module is not None and not isinstance(module, str):
            raise ValueError("invalid_module")
        view.assets = [SelectedAsset(
            protocol_version=protocol, asset_kind="ctf_token" if protocol == "v1" else "position",
            asset_id=asset_id, ledger="ConditionalTokens" if protocol == "v1" else "PositionManager",
            chain_id=str(raw["chainId"]) if raw.get("chainId") is not None else None,
            event_id=str(event_id), market_id=str(raw.get("id", "")),
            condition_id=condition.lower(), raw_condition_id=condition,
            outcome=label, outcome_index=i, module=module, sdk_version=SDK_VERSION,
            observed_at=observed_at, received_at=received_at,
        ) for i, (label, asset_id) in enumerate(zip(labels, ids))]
        view.status, view.reason = "selected", None
    except ValueError as exc:
        view.reason = str(exc)
    return view


def require_assets(view: AssetSelection):
    if view.status != "selected" or not view.assets:
        raise ValueError(view.reason or "quarantined_identity")
    return view.assets


def stream_key(asset: SelectedAsset):
    """Ledger-scoped checkpoint identity; legacy bare-ID cursors are not reused."""
    return f"{asset.chain_id or 'unknown'}:{asset.ledger}:{asset.asset_id}"

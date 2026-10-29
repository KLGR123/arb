"""Archive open markets, predict without outcomes, then join public settlements."""

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean

from .context import build_context, canonical, digest, probability, prompt_for, timestamp
from .metrics import evaluate
from .public import array, binary_truth
from .runner import AuditStore, now


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".partial")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n")
    temporary.replace(path)


def archive_open(client, output, *, count=56, min_minutes=10, max_minutes=45):
    if not 1 <= count <= 1000 or not 0 <= min_minutes < max_minutes <= 120:
        raise ValueError("Invalid prospective selection bounds")
    start = datetime.now(timezone.utc)
    result = {"schema_version": 1, "mode": "prospective_archive", "started_at": now(),
              "selection": {"count": count, "min_minutes": min_minutes, "max_minutes": max_minutes,
                            "policy": "predeclared short-cycle assets, earliest expiry, interior recent trade",
                            "assets": ["btc", "eth", "sol", "xrp", "doge", "bnb", "zec", "hype"]},
              "compliance": client.compliance_status(), "markets": [], "excluded": []}
    # Gamma caps a page at 100. Query explicit short-cycle slugs rather than
    # letting hundreds of football score variants crowd out the target cohort.
    slugs = []
    for width in (300, 900):
        for epoch in range(int(start.timestamp()) // width * width,
                           int(start.timestamp()) + max_minutes * 60, width):
            until_end = epoch + width - start.timestamp()
            if min_minutes * 60 <= until_end <= max_minutes * 60:
                slugs.extend(f"{asset}-updown-{width // 60}m-{epoch}"
                             for asset in result["selection"]["assets"])
    rows = []
    for offset in range(0, len(slugs), 80):
        rows.extend(client.get("markets", slug=slugs[offset:offset + 80], limit=100))
    rows.sort(key=lambda m: (m["endDate"], str(m["id"])))
    captured = now()
    for gamma in rows:
        try:
            labels = array(gamma["outcomes"])
            if labels != ["Up", "Down"] or gamma.get("closed") is not False:
                continue
            tokens = array(gamma["clobTokenIds"])
            if len(tokens) != 2 or timestamp(gamma["endDate"]) <= datetime.now(timezone.utc):
                continue
            at = datetime.now(timezone.utc)
            # as_of on an OPEN market returns a raw trade (resolution_seconds may
            # be zero). Unlike interval history this is not a settlement point.
            response = client.get("prices", token_id=tokens[0], as_of=int(at.timestamp()))
            quote_captured = now()
            points = [p for p in response["data"]
                      if p["timestamp"] <= at.timestamp() and 0 < probability(p["price"]) < 1]
            if not points:
                raise ValueError("No interior pre-forecast trade")
            point = max(points, key=lambda p: p["timestamp"])
            q_at = datetime.fromtimestamp(point["timestamp"], timezone.utc).isoformat()
            market = {
                "id": str(gamma["id"]), "condition_id": gamma["conditionId"],
                "source": "polymarket-prospective-v2", "target_outcome": labels[0],
                "slug": gamma["slug"],
                "outcomes": labels, "yes_token_id": tokens[0], "cluster_id": gamma["endDate"],
                "created_at": gamma["createdAt"], "end_at": gamma["endDate"],
                "forecast_at": quote_captured, "snapshot_closed": False,
                "metadata_snapshots": [{"question": gamma["question"],
                                        "rules": f"YES denotes {labels[0]}; NO denotes {labels[1]}.\n" + gamma["description"],
                                        "available_at": captured, "captured_at": captured}],
                "prices": [{"timestamp": q_at, "available_at": quote_captured,
                            "retrieved_at": quote_captured, "kind": "trade", "p": point["price"],
                            "provenance": "Data V2 as_of while open and before scheduled expiry"}],
                "evidence": [], "quote_receipt": response,
            }
            build_context(market)
            result["markets"].append(market)
        except (KeyError, TypeError, ValueError) as exc:
            result["excluded"].append({"market_id": str(gamma.get("id")), "reason": str(exc)})
        save(output, result)
        if len(result["markets"]) >= count:
            break
    result["completed_at"] = now()
    result["archive_sha256"] = digest(result["markets"])
    save(output, result)
    return result


def predict_archive(archive, predictor, store: AuditStore, output, *, samples=2, workers=4):
    markets = archive["markets"]
    if len(markets) < 50 or samples < 1 or not 1 <= workers <= 4:
        raise ValueError("Require at least 50 archived markets, positive samples, workers <=4")
    if len({m["id"] for m in markets}) != len(markets):
        raise ValueError("Duplicate archive markets")
    if any(timestamp(predictor.training_cutoff) > timestamp(m["forecast_at"]) for m in markets):
        raise ValueError("Model training cutoff exceeds forecast time")
    if any("y" in m or "resolved_at" in m for m in markets):
        raise ValueError("Prospective archive already contains outcomes")
    config = {"archive_sha256": digest(markets), "model": predictor.model,
              "samples": samples, "mode": "prospective_model"}
    run_id = store.start(config)
    result = {"schema_version": 1, "config": config, "predictions": [], "failures": []}

    def one(market, sample):
        context = build_context(market)
        prompt = prompt_for(context)
        receipt = {"market_id": market["id"], "model": predictor.model, "sample": sample,
                   "forecast_at": market["forecast_at"], "retrieved_at": now(),
                   "prompt": prompt, "prompt_sha256": digest(prompt),
                   "context_sha256": digest(context), "context": context,
                   "training_cutoff": predictor.training_cutoff}
        try:
            if timestamp(now()) >= timestamp(market["end_at"]):
                raise ValueError("Forecast deadline passed before invocation")
            response = predictor.predict(prompt, sample=sample)
            if timestamp(now()) >= timestamp(market["end_at"]):
                raise ValueError("Forecast completed after scheduled expiry")
            receipt.update(status="succeeded", predicted_at=now(), response={"p": probability(response["p"])},
                           model_audit=response.get("audit", {}))
        except Exception as exc:
            receipt.update(status="failed", predicted_at=now(), error_type=type(exc).__name__)
        store.prediction(run_id, market["id"], predictor.model, sample, receipt)
        return receipt

    # Pool is joined in the foreground, never left running when this function returns.
    with ThreadPoolExecutor(max_workers=workers) as pool:
        tasks = [pool.submit(one, m, sample) for m in markets for sample in range(samples)]
        for task in as_completed(tasks):
            receipt = task.result()
            result["predictions" if receipt["status"] == "succeeded" else "failures"].append(receipt)
            save(output, result)
            print(canonical({"completed": len(result["predictions"]), "failed": len(result["failures"]),
                             "total": len(tasks)}), flush=True)
    result["predictions"].sort(key=lambda r: (r["market_id"], r["model"], r["sample"]))
    result["completed_at"] = now()
    store.finish(run_id, {"status": "failed" if result["failures"] else "predicted",
                          "predictions_sha256": digest(result["predictions"])})
    save(output, result)
    return result


def join_settlements(client, archive, predictions, output):
    """One foreground refresh, no hidden sleep or automatic trading action."""
    if predictions["config"]["archive_sha256"] != digest(archive["markets"]):
        raise ValueError("Prediction archive hash mismatch")
    result = {"markets": [], "pending": [], "excluded": [], "checked_at": now(),
              "archive_sha256": digest(archive["markets"]),
              "archive_market_hashes": {m["id"]: digest(m) for m in archive["markets"]}}
    by_id = {}
    for p in predictions["predictions"]:
        by_id.setdefault(p["market_id"], []).append(p)
    for offset in range(0, len(archive["markets"]), 20):
        batch = archive["markets"][offset:offset + 20]
        rows = client.get("resolutions", condition=",".join(m["condition_id"] for m in batch))["data"]
        resolved = {r["condition_id"].lower(): r for r in rows}
        for market in batch:
            resolution = resolved.get(market["condition_id"].lower())
            # A batch can omit an already published condition. After expiry,
            # make one bounded canonical single-condition lookup before pending.
            if (not resolution or resolution.get("status", "").upper() != "RESOLVED") and (
                    timestamp(now()) >= timestamp(market["end_at"])):
                single = client.get("resolutions", condition=market["condition_id"])["data"]
                matches = [r for r in single if r["condition_id"].lower() == market["condition_id"].lower()]
                if len(matches) == 1:
                    resolution = matches[0]
            if not resolution or resolution.get("status", "").upper() != "RESOLVED":
                result["pending"].append(market["id"])
                continue
            try:
                y = binary_truth(resolution["payouts"])
                complete = {**market, "y": y,
                            "resolved_at": resolution["resolved_at"], "resolution_receipt": resolution}
                validate_records(complete, by_id.get(market["id"], []), predictions["config"])
                result["markets"].append(complete)
            except (KeyError, ValueError) as exc:
                result["excluded"].append({"market_id": market["id"], "reason": str(exc)})
    save(output, result)
    return result


def validate_records(market, records, config):
    expected = config["samples"]
    if len(records) != expected or {r["sample"] for r in records} != set(range(expected)):
        raise ValueError("Incomplete or duplicate prediction samples")
    deadline = min(timestamp(market["resolved_at"]), timestamp(market["end_at"]))
    if any(timestamp(p["predicted_at"]) >= deadline for p in records):
        raise ValueError("Prediction completed after expiry or resolution")
    resolution = market["resolution_receipt"]
    if (resolution["condition_id"].lower() != market["condition_id"].lower()
            or resolution["status"].upper() != "RESOLVED"
            or resolution["resolved_at"] != market["resolved_at"]
            or binary_truth(resolution["payouts"]) != market["y"]):
        raise ValueError("Settlement receipt or truth mismatch")
    context = build_context(market)
    prompt = prompt_for(context)
    for record in records:
        if (record["model"] != config["model"] or record["prompt_sha256"] != digest(prompt)
                or record["prompt"] != prompt or record["context"] != context
                or record["context_sha256"] != digest(context)):
            raise ValueError("Archived model, context or prompt changed")
        if timestamp(record["training_cutoff"]) > timestamp(market["forecast_at"]):
            raise ValueError("Model training cutoff exceeds forecast time")
        audit = record.get("model_audit", {})
        if audit.get("provider") == "local_codex_cli":
            if (audit["model"] != config["model"] or audit["prompt_sha256"] != digest(prompt)
                    or audit["tool_calls"] != 0 or audit["sample"] != record["sample"]
                    or not timestamp(market["forecast_at"]) <= timestamp(audit["started_at"])
                    <= timestamp(audit["completed_at"]) <= timestamp(record["predicted_at"])):
                raise ValueError("Model invocation audit mismatch")
        probability(record["response"]["p"])
    return context


def score_joined(joined, predictions):
    if len(joined["markets"]) < 50:
        raise ValueError("Need at least 50 fully predicted settled markets")
    if len({m["id"] for m in joined["markets"]}) != len(joined["markets"]):
        raise ValueError("Duplicate settled markets")
    original = [{k: v for k, v in m.items() if k not in ("y", "resolved_at", "resolution_receipt")}
                for m in joined["markets"]]
    if joined["archive_sha256"] != predictions["config"]["archive_sha256"]:
        raise ValueError("Settled archive binding mismatch")
    if any(digest(m) != joined["archive_market_hashes"].get(m["id"]) for m in original):
        raise ValueError("Settled market archive changed")
    rows = []
    for m in joined["markets"]:
        records = [r for r in predictions["predictions"] if r["market_id"] == m["id"]]
        context = validate_records(m, records, predictions["config"])
        rows.append({"market_id": m["id"], "cluster_id": m["cluster_id"],
                     "forecast_at": m["forecast_at"], "quote_at": context["quote_at"],
                     "p": mean(r["response"]["p"] for r in records), "q": context["market_price"],
                     "y": m["y"], "samples": len(records)})
    return {"schema_version": 1, "mode": "prospective_model", "rows": rows,
            "config": predictions["config"], "metrics": evaluate(rows),
            "archive_sha256": joined["archive_sha256"],
            "settled_markets_sha256": digest(joined["markets"]),
            "predictions_sha256": digest(predictions["predictions"]),
            "excluded": joined["excluded"]}


def archive_history(client, archive, output):
    """Record whole-life history separately; never augment a past model context."""
    result = {"source": "Polymarket Data V2 interval history",
              "retrieval_purpose": "audit only; never included in prediction context",
              "started_at": now(), "markets": []}
    for market in archive["markets"]:
        pages, cursor, seen = [], None, set()
        for _ in range(100):
            params = {"token_id": market["yes_token_id"], "interval": "max", "bucket_seconds": 600}
            if cursor:
                params["cursor"] = cursor
            response = client.get("prices", **params)
            pages.append(response)
            if not response["pagination"]["has_more"]:
                break
            cursor = response["pagination"].get("next_cursor")
            if not cursor or cursor in seen:
                raise ValueError("Invalid or repeated history cursor")
            seen.add(cursor)
        else:
            raise ValueError("History pagination exceeded 100 pages")
        result["markets"].append({"market_id": market["id"], "yes_token_id": market["yes_token_id"],
                                  "retrieved_at": now(), "pages": pages})
        save(output, result)
    result["completed_at"] = now()
    save(output, result)
    return result

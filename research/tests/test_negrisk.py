"""Offline financial invariants and API boundary tests; never calls the network."""

import copy

from negrisk_legacy import legacy_report
import json
from pathlib import Path
import tempfile
import unittest

import httpx
from unittest.mock import patch
from urllib.error import HTTPError

from arb_research.negrisk import Book, Costs, D, Leg, assets, dumps, fee_config, opportunity, scan, taker_fee, timestamp
from arb_research.negrisk_cli import ReadOnlyAPI, TokenBucket, collect, main


FIXTURE = Path(__file__).parent / "fixtures" / "negrisk-contract.json"


class ScannerTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = json.loads(FIXTURE.read_text())

    def test_fee_curve_rounding_and_invalid_numbers(self):
        self.assertEqual(taker_fee(100, ".07", ".5"), D("1.75000"))
        self.assertEqual(taker_fee(100, ".05", ".3"), D("1.05000"))
        self.assertEqual(taker_fee(100, ".07", ".3"), taker_fee(100, ".07", ".7"))
        self.assertEqual(taker_fee(".00001", ".07", ".001"), D(0))
        self.assertEqual(taker_fee(1, ".00002", ".5"), D(".00001"))
        for values in ((-1, .07, .5), (1, .07, 2), (1, "NaN", .5)):
            with self.assertRaises(ValueError):
                taker_fee(*values)

    def test_fee_source_priority_and_non_linear_rejection(self):
        market = self.snapshot["events"][0]["markets"][0]
        self.assertEqual(fee_config(market, {}, {"fd": {"r": ".03", "e": 1}}), (D(".03"), "clob"))
        self.assertEqual(fee_config({"feesEnabled": False}, {}, {}), (D(0), "fees_disabled"))
        self.assertEqual(fee_config({"feesEnabled": True, "category": "crypto"}, {}, {})[0], D(".07"))
        for m, c in (({}, {}), ({"feesEnabled": True}, {}), (market, {"fd": {"r": ".07", "e": 2}})):
            with self.assertRaises(ValueError):
                fee_config(m, {}, c)

    def test_outcome_mapping_and_protocol_version(self):
        self.assertEqual(assets({"version": "v1", "conditionId": "0xcondition", "outcomes": '["No", "Yes"]', "clobTokenIds": '["9", "8"]'}), {"no": "9", "yes": "8"})
        market = {"version": "v2", "conditionId": "0xcondition", "outcomes": '["Yes", "No"]', "positionIds": ["1", "2"], "clobTokenIds": '["9", "8"]'}
        self.assertEqual(assets(market), {"yes": "1", "no": "2"})
        with self.assertRaises(ValueError):
            assets({"version": "v1", "conditionId": "0xcondition", "outcomes": '["Yes", "No"]', "clobTokenIds": '["1", "1"]'})

    def test_book_ordering_and_depth(self):
        raw = copy.deepcopy(self.snapshot["books"]["101"])
        raw["asks"] = [{"price": ".8", "size": "3"}, {"price": ".2", "size": "4"}, {"price": ".2", "size": "6"}]
        raw["bids"] = [{"price": ".1", "size": "3"}, {"price": ".19", "size": "2"}]
        book = Book.parse("101", raw, timestamp(self.snapshot["as_of"]), 120)
        self.assertEqual(book.asks[0], (D(".2"), D(10)))
        self.assertEqual(book.bids[0][0], D(".19"))
        # At ten shares profit is 8; at thirteen, 8.6. Fixed merge cost 1.
        leg = Leg("1", "yes", "buy", book, D(0))
        row = opportunity("unit", "1", [leg], D(1), D(1), [], D(100), 10)
        self.assertEqual(row["quantity"], D(13))
        self.assertEqual(row["net_profit_usd"], D("7.6"))

    def test_depth_optimum_does_not_consume_unprofitable_tail(self):
        books = []
        for asset_id in ("101", "102"):
            raw = copy.deepcopy(self.snapshot["books"][asset_id])
            raw["asks"] = [{"price": ".4", "size": "10"}, {"price": ".7", "size": "90"}]
            raw["bids"] = [{"price": ".39", "size": "100"}]
            books.append(Book.parse(asset_id, raw, timestamp(self.snapshot["as_of"]), 120))
        legs = [Leg("1", outcome, "buy", book, D(".07")) for outcome, book in zip(("yes", "no"), books)]
        row = opportunity("binary", "1", legs, D(1), D(".1"), [], D(1000), 10)
        self.assertEqual(row["quantity"], D(10))
        self.assertEqual(row["depth_capacity_shares"], D(100))
        self.assertEqual(row["net_profit_usd"], D("1.56400"))
        self.assertIsNone(opportunity("binary", "1", legs, D(1), D(3), [], D(1000), 10))

    def test_duplicate_condition_and_mismatched_or_crossed_book_are_rejected(self):
        for kind in ("duplicate", "condition", "crossed", "event_membership"):
            with self.subTest(kind=kind):
                snapshot = copy.deepcopy(self.snapshot)
                if kind == "duplicate":
                    snapshot["events"][0]["markets"].append(copy.deepcopy(snapshot["events"][0]["markets"][0]))
                elif kind == "condition":
                    snapshot["books"]["101"]["market"] = "0xwrong"
                elif kind == "crossed":
                    snapshot["books"]["101"]["bids"][0]["price"] = ".99"
                else:
                    snapshot["events"][0]["negRiskMarketID"] = "0xdifferent"
                report = scan(snapshot)
                self.assertFalse(report["groups"][0]["complete"])
                self.assertTrue(all(r["kind"] == "buy_binary_merge" for r in report["opportunities"]))

    def test_basket_binary_and_conversion_math(self):
        report = scan(self.snapshot, Costs(merge_usd=D(".1"), convert_usd=D(".2")))
        basket = next(r for r in report["opportunities"] if r["kind"] == "buy_yes_basket")
        self.assertEqual(basket["quantity"], D(20))
        self.assertEqual(basket["gross_profit_usd"], D(5))
        self.assertEqual(basket["taker_fees_usd"], D(".78050"))
        binary = next(r for r in report["opportunities"] if r["kind"] == "buy_binary_merge" and r["legs"][0]["market_id"] == "1")
        self.assertEqual(binary["operation_cost_usd"], D(".1"))
        converted = next(r for r in report["opportunities"] if r["kind"] == "no_to_yes")
        # Buy NO(c)=.35, convert, sell YES(a)+YES(b)=.19+.24=.43.
        self.assertEqual(converted["gross_profit_usd"], D("1.60"))
        self.assertEqual(converted["operation_cost_usd"], D(".2"))
        edges = [r["net_edge_per_share"] for r in report["opportunities"]]
        self.assertEqual(edges, sorted(edges, reverse=True))
        self.assertEqual(report["groups"][0]["yes_ask_deviation"], D("-.25"))

    def test_split_and_conversion_fee_scale_all_legs(self):
        for asset, raw in self.snapshot["books"].items():
            if asset.endswith("1"):
                raw["bids"] = [{"price": ".4", "size": "20"}]
                raw["asks"] = [{"price": ".41", "size": "20"}]
        costs = Costs(split_usd=D(".1"), convert_usd=D(".2"), conversion_fee_bps=D(100))
        report = scan(self.snapshot, costs)
        row = next(r for r in report["opportunities"] if r["kind"] == "split_sell_yes_basket")
        self.assertEqual(sorted(leg["shares"] for leg in row["legs"]), [D("19.8"), D("19.8"), D(20)])
        self.assertEqual(row["gross_profit_usd"], D("3.84"))
        self.assertEqual(row["operation_cost_usd"], D(".3"))
        # NO conversion uses reduced output ratio and cannot become more profitable.
        base = scan(self.snapshot, Costs())
        base_row = next(r for r in base["opportunities"] if r["kind"] == "split_sell_yes_basket")
        self.assertLess(row["net_profit_usd"], base_row["net_profit_usd"])

    def test_incomplete_augmented_other_and_membership_are_not_exhaustive(self):
        for mutation in ("missing", "closed", "augmented", "other", "placeholder", "membership"):
            with self.subTest(mutation=mutation):
                snapshot = copy.deepcopy(self.snapshot)
                event = snapshot["events"][0]
                if mutation == "missing":
                    del snapshot["books"]["101"]
                elif mutation == "closed":
                    event["markets"][0]["closed"] = True
                elif mutation == "augmented":
                    event["negRiskAugmented"] = True
                elif mutation == "membership":
                    event["markets"][0]["negRiskMarketID"] = "different"
                else:
                    event["markets"][0]["negRiskOther" if mutation == "other" else "negRiskPlaceholder"] = True
                report = scan(snapshot)
                self.assertFalse(report["groups"][0]["complete"])
                self.assertIsNone(report["groups"][0]["yes_ask_sum"])
                self.assertTrue(all(r["kind"] == "buy_binary_merge" for r in report["opportunities"]))

    def test_stale_skew_empty_minimum_and_risks(self):
        for kind in ("stale", "future", "empty", "minimum", "skew"):
            with self.subTest(kind=kind):
                snapshot = copy.deepcopy(self.snapshot)
                for raw in snapshot["books"].values():
                    if kind == "stale":
                        raw["timestamp"] = "2026-10-01T00:00:00Z"
                    elif kind == "future":
                        raw["timestamp"] = "2027-01-01T00:00:00Z"
                    elif kind == "empty":
                        raw["asks"] = []
                    elif kind == "minimum":
                        raw["min_order_size"] = "1000"
                if kind == "skew":
                    # Alternate each YES/NO and between markets to exceed max skew.
                    for asset in ("101", "202", "301"):
                        snapshot["books"][asset]["timestamp"] = "2026-10-29T23:59:20Z"
                self.assertEqual(scan(snapshot)["opportunities"], [])
        event = self.snapshot["events"][0]
        event["markets"][0]["endDate"] = "2026-10-30T12:00:00Z"
        self.assertIn("near_resolution", scan(self.snapshot)["groups"][0]["markets"][0]["risks"])

    def test_json_cli_roundtrip_is_deterministic_and_offline(self):
        with tempfile.TemporaryDirectory() as directory, patch("socket.socket", side_effect=AssertionError("network forbidden")):
            out, md = Path(directory) / "report.json", Path(directory) / "report.md"
            self.assertEqual(main(["--fixture", str(FIXTURE), "--json", str(out), "--markdown", str(md)]), 0)
            first = out.read_bytes()
            self.assertEqual(main(["--fixture", str(FIXTURE), "--json", str(out)]), 0)
            self.assertEqual(first, out.read_bytes())
            report = json.loads(first)
            self.assertEqual(report["schema_version"], "1.1")
            self.assertIsInstance(report["opportunities"][0]["quantity"], str)
            self.assertIn("execution is always blocked", md.read_text())
        self.assertEqual(dumps(scan(self.snapshot)), dumps(scan(self.snapshot)))

    def test_recorded_live_scan_replays_documented_report_without_network(self):
        source = FIXTURE.parent / "negrisk-live-20261029.json"
        expected = Path(__file__).resolve().parents[2] / "docs/reports/negrisk-2026-10-29.json"
        with patch("socket.socket", side_effect=AssertionError("network forbidden")):
            report = scan(json.loads(source.read_text()))
        historical = json.loads(expected.read_text())
        historical["schema_version"] = "1.1"
        self.assertEqual(legacy_report(json.loads(dumps(report)), historical), historical)
        self.assertEqual(report["summary"], {"groups_scanned": 2, "opportunities": 0, "errors": 0})
        self.assertEqual(sum(len(g["markets"]) for g in report["groups"]), 10)
        self.assertEqual(report["groups"][1]["yes_ask_sum"], D("1.019"))
        self.assertEqual(report["groups"][1]["yes_bid_sum"], D(".995"))
        self.assertTrue(report["compliance"]["geoblock"]["blocked"])
        self.assertFalse(report["compliance"]["trading_allowed"])

    def test_replay_input_cannot_report_execution_permission(self):
        self.snapshot["geoblock"]["execution"] = "allowed"
        self.snapshot["compliance"] = {"trading_allowed": True, "allowlist_configured": True, "mode": "live"}
        report = scan(self.snapshot)
        self.assertEqual(report["geoblock"]["execution"], "blocked")
        self.assertFalse(report["compliance"]["trading_allowed"])
        self.assertFalse(report["compliance"]["allowlist_configured"])


class TransportTests(unittest.TestCase):
    def test_bucket_refill(self):
        now, slept = [0.0], []
        def sleep(seconds):
            slept.append(seconds)
            now[0] += seconds
        bucket = TokenBucket(rate=2, capacity=2, clock=lambda: now[0], sleep=sleep)
        for _ in range(4):
            bucket.acquire()
        self.assertEqual(slept, [.5, .5])

    def test_get_allowlist_retry_and_no_retry_on_403(self):
        calls, sleeps = [], []
        def handler(request):
            calls.append(request)
            self.assertEqual(request.method, "GET")
            self.assertNotIn("Authorization", request.headers)
            if len(calls) == 1:
                return httpx.Response(429, headers={"Retry-After": "2"})
            return httpx.Response(200, json={"bids": [], "asks": []})
        with httpx.Client(transport=httpx.MockTransport(handler), trust_env=False) as client:
            api = ReadOnlyAPI(client=client, sleep=sleeps.append)
            with patch("arb_research.negrisk_cli.PUBLIC_API_BUDGET.acquire") as global_budget:
                self.assertEqual(api.get("clob", "/book", {"token_id": "101"}), {"bids": [], "asks": []})
                self.assertEqual(global_budget.call_count, 2)
            self.assertEqual(len(calls), 2)
            self.assertGreaterEqual(sleeps[0], 2)
            for service, path in (("clob", "/order"), ("gamma", "/events/../order"), ("other", "/book")):
                with self.assertRaises(ValueError):
                    api.get(service, path)
        for code in (403, 302):
            calls.clear()
            def denied(request):
                calls.append(request)
                return httpx.Response(code, headers={"Location": "https://example.com"})
            with httpx.Client(transport=httpx.MockTransport(denied), trust_env=False) as client:
                api = ReadOnlyAPI(client=client)
                with self.assertRaises((HTTPError, ValueError)):
                    api.get("clob", "/book")
            self.assertEqual(len(calls), 1)

    def test_proxy_is_explicit_scoped_and_transport_errors_are_redacted(self):
        sentinel = "socks5h://fixture-user:fixture-secret@localhost:9999"
        with patch.dict("os.environ", {"ARB_POLYMARKET_PROXY": sentinel, "HTTPS_PROXY": "http://unused.invalid"}), patch(
                "arb_research.negrisk_cli.httpx.Client") as factory:
            api = ReadOnlyAPI()
            self.assertEqual(factory.call_args.kwargs["proxy"], sentinel)
            self.assertFalse(factory.call_args.kwargs["trust_env"])
            self.assertFalse(factory.call_args.kwargs["follow_redirects"])
            self.assertEqual(__import__("os").environ["HTTPS_PROXY"], "http://unused.invalid")
            api.close()
        def fails(request):
            raise httpx.ConnectError(sentinel, request=request)
        with httpx.Client(transport=httpx.MockTransport(fails), trust_env=False) as client:
            api = ReadOnlyAPI(client=client, attempts=1)
            with self.assertRaises(OSError) as exc:
                api.get("clob", "/book")
            self.assertEqual(str(exc.exception), "public_read_transport_failed")
            self.assertNotIn(sentinel, str(exc.exception))

    def test_discovery_is_bounded_deduplicated_and_geo_is_sanitized(self):
        snapshot = json.loads(FIXTURE.read_text())
        class API:
            calls = []
            def get(self, service, path, params=None):
                self.calls.append((service, path, params))
                if service == "geo":
                    return {"blocked": True, "country": "US", "region": "VA", "ip": "private"}
                if service == "gamma":
                    return [snapshot["events"][0], snapshot["events"][0]]
                if path == "/book":
                    return snapshot["books"][params["token_id"]]
                return {"fd": {"r": ".07", "e": 1}}
        api = API()
        result = collect(api, max_events=1)
        self.assertEqual(len(result["events"]), 1)
        self.assertEqual(len(result["books"]), 6)
        self.assertEqual(result["geoblock"], {"status": "blocked", "country": "US", "region": "VA", "execution": "blocked"})
        self.assertNotIn("private", dumps(result))
        self.assertEqual(result["coverage"]["pages_read"], 1)
        self.assertFalse(result["compliance"]["trading_allowed"])
        self.assertTrue(result["compliance"]["geoblock"]["blocked"])

    def test_failed_scan_writes_error_report_and_exits_nonzero(self):
        api = unittest.mock.Mock()
        api.get.side_effect = TimeoutError("direct connection timed out")
        snapshot = collect(api)
        self.assertEqual(snapshot["events"], [])
        self.assertEqual(len(snapshot["errors"]), 2)
        with tempfile.TemporaryDirectory() as directory:
            source, report = Path(directory) / "input.json", Path(directory) / "output.json"
            source.write_text(dumps(snapshot))
            self.assertEqual(main(["--fixture", str(source), "--json", str(report)]), 2)
            self.assertEqual(json.loads(report.read_text())["geoblock"]["status"], "unknown")


if __name__ == "__main__":
    unittest.main()

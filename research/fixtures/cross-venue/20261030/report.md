# Cross-venue paper research

As of: 2026-10-30T01:37:51.081810+00:00. Execution: blocked.
All edges are conditional estimates, subject to human settlement-rule review.

| Polymarket / Kalshi | Rule relation | Risk | Differences / risks |
| --- | --- | --- | --- |
| Will the Fed decrease interest rates by 50+ bps after the December 2026 meeting? / Will the upper bound of the federal funds rate be above 3.50% following the Fed's Dec 9, 2026 meeting? Above 3.50% | different | critical | Kalshi tests an absolute post-meeting rate; Polymarket tests the size and direction of the change from the prior rate.; Polymarket specifies a no-statement fallback to No change; Kalshi gives an expiration deadline but does not specify the resulting payout if no statement is released.; The contracts have different listed close and settlement times, and different rule-based deadlines.; Cancellation, ties, and 50/50 payouts are not specified in the supplied rules.; kalshi_fee_schedule_estimate; model_requires_human_rule_review; regional_restrictions_research_only; rules_different_or_unverified; settlement_time_gap; thin_or_missing_top_level_depth |
| Will the Fed decrease interest rates by 50+ bps after the December 2026 meeting? / Will the upper bound of the federal funds rate be above 4.50% following the Fed's Dec 9, 2026 meeting? Above 4.50% | different | critical | Kalshi tests the absolute post-meeting rate; Polymarket tests the rate change from its pre-meeting level.; Polymarket specifies rounding and a no-statement fallback; Kalshi's supplied rules do not specify matching provisions.; The expiration and fallback timing differ.; Neither supplied rule specifies cancellation, tie, or 50/50 payout treatment.; kalshi_fee_schedule_estimate; model_requires_human_rule_review; regional_restrictions_research_only; rules_different_or_unverified; settlement_time_gap; thin_or_missing_top_level_depth |
| Will the Fed decrease interest rates by 25 bps after the December 2026 meeting? / Will the upper bound of the federal funds rate be above 3.25% following the Fed's Dec 9, 2026 meeting? Above 3.25% | different | critical | Kalshi tests an absolute post-meeting rate; Polymarket tests the size and direction of the change from the pre-meeting rate.; For example, a decrease from 3.50% to 3.25% would be Polymarket YES but Kalshi NO.; Polymarket specifies a no-statement fallback to No change; Kalshi specifies a one-week expiration but does not state the resulting payout in that circumstance.; Kalshi specifies 2:05 PM Eastern Time; Polymarket's rule does not specify a timezone for the next-meeting end-date fallback.; Neither supplied rule specifies cancellation, tie, or 50/50 payout treatment.; kalshi_fee_schedule_estimate; model_requires_human_rule_review; regional_restrictions_research_only; rules_different_or_unverified; settlement_time_gap; thin_or_missing_top_level_depth |

Ask-price differences (Polymarket minus Kalshi, USD/share; not payout equivalence):

| Pair | YES/YES | YES/NO | NO/YES | NO/NO |
| --- | ---: | ---: | ---: | ---: |
| 3215005 / KXFED-26DEC-T3.50 | unavailable | -0.0060 | unavailable | 0.9870 |
| 3215005 / KXFED-26DEC-T4.50 | -0.0060 | unavailable | 0.9870 | unavailable |
| 3215006 / KXFED-26DEC-T3.25 | unavailable | -0.0040 | unavailable | 0.9850 |

| Pair | Paper scenario | Outcomes P/K | Shares | Net USD/share |
| --- | --- | --- | ---: | ---: |

Coverage: {'pages_limit': 1, 'pair_limit': 3, 'max_close_gap_hours': 48, 'min_title_similarity': 0.2, 'selection': 'explicit_events', 'poly_markets': 5, 'kalshi_markets': 11, 'selected_pairs': 3, 'exhaustive': False}
Data errors: 0.
Kalshi China restriction and venue eligibility remain separate from public data access.

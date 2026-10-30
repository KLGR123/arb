"""Protocol/module gates before transport and before replay valuation."""

import copy
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from arb_research.negrisk import Costs, D, assets, dumps, scan
from arb_research.negrisk_cli import collect, main, markdown
from arb_research.negrisk_deep import deep_scan
from arb_research.polymarket.assets import require_assets, select_assets

FIXTURES = Path(__file__).parent / 'fixtures'
CASES = json.loads((FIXTURES / 'negrisk-rejections.json').read_text())


def snapshot(protocol='v1'):
    name = 'negrisk-v2-contract.json' if protocol == 'v2' else 'negrisk-contract.json'
    return json.loads((FIXTURES / name).read_text())


class API:
    def __init__(self, raw):
        self.raw, self.calls = raw, []

    def get(self, service, path, params=None):
        self.calls.append((service, path, params))
        if service == 'geo':
            return dict(blocked=True, country='US', region='CA')
        if service == 'gamma':
            return copy.deepcopy(self.raw['events'])
        if path == '/book':
            return copy.deepcopy(self.raw['books'][params['token_id']])
        return {'fd': {'r': '.07', 'e': 1}}


@pytest.mark.parametrize('case', CASES, ids=lambda c: c['name'])
def test_rejected_market_never_requests_clob_and_replay_cannot_bypass(case):
    raw = snapshot()
    market = raw['events'][0]['markets'][0]
    market.update(case['patch'])
    api = API(raw)
    collected = collect(api, max_events=1)
    requested = [params['token_id'] for _, path, params in api.calls if path == '/book']
    assert set(requested) == {'201', '202', '301', '302'}
    assert not any(path == '/clob-markets/0xcondition1' for _, path, _ in api.calls)
    rejected = next(e for e in collected['errors'] if e.get('market_id') == '1')
    assert rejected['reason'] == case['reason']
    assert rejected['version'] == market.get('version')
    assert rejected['module'] == (market.get('module') or market.get('moduleAddress'))
    assert 'raw_identity' in rejected and 'assets' in rejected
    # Replay already contains the old books. It must still reject this market.
    report = scan(raw)
    group = report['groups'][0]
    assert not group['complete'] and group['conversion_ratio'] is None
    assert group['skipped_markets'][0]['reason'] == case['reason']
    assert group['skipped_reasons'] == ['incomplete_negrisk_group']
    assert all(row['kind'] == 'buy_binary_merge' for row in report['opportunities'])
    assert all(leg['market_id'] != '1' for row in report['opportunities'] for leg in row['legs'])
    assert 'incomplete_negrisk_group' in markdown(report)
    assert collected['compliance']['trading_allowed'] is False


@pytest.mark.parametrize('protocol', ['v1', 'v2'])
def test_binary_basket_conversion_numbers_and_full_identity(protocol):
    raw = snapshot(protocol)
    report = scan(raw, Costs(merge_usd=D('.1'), convert_usd=D('.2'), conversion_fee_bps=D('100')))
    assert not report['errors'] and report['groups'][0]['complete']
    assert report['groups'][0]['conversion_ratio'] == D('.99')
    basket = next(r for r in report['opportunities'] if r['kind'] == 'buy_yes_basket')
    assert (basket['quantity'], basket['gross_profit_usd'], basket['taker_fees_usd']) == (D(20), D(5), D('.78050'))
    binary = next(r for r in report['opportunities'] if r['kind'] == 'buy_binary_merge' and r['legs'][0]['market_id'] == '1')
    assert binary['net_profit_usd'] == D('3.34000')  # 20*(1-.20-.60) - .224-.336-.1
    converted = next(r for r in report['opportunities'] if r['kind'] == 'no_to_yes' and r['anchor_market_id'] == '3')
    assert converted['gross_profit_usd'] == D('1.5140')  # .99*20*(.19+.24)-20*.35
    for market in report['groups'][0]['markets']:
        assert market['asset_selection']['protocol_version'] == protocol
    for row in report['opportunities']:
        for leg in row['legs']:
            assert leg['protocol_version'] == protocol
            assert leg['asset_kind'] == ('position' if protocol == 'v2' else 'ctf_token')
            assert isinstance(leg['asset_id'], str)
            assert leg['module'] == ('neg_risk' if protocol == 'v2' else 'v1_ctf')
    # Consumers choose the identical IDs even when the other ID family exists.
    for m in raw['events'][0]['markets']:
        assert set(assets(m).values()) == {a.asset_id for a in require_assets(select_assets(m))}
    api = API(raw)
    collect(api, max_events=1)
    requested = {params['token_id'] for _, path, params in api.calls if path == '/book'}
    assert requested == set(raw['books'])
    if protocol == 'v2':
        assert all(int(x) > 2**53 for x in requested)
        assert requested == {str(x) for x in requested}


@pytest.mark.parametrize('change', ['mixed', 'wrong-group', 'event-group', 'single', 'augmented', 'other', 'placeholder', 'duplicate'])
def test_incomplete_groups_have_explicit_reason_and_no_conversion(change):
    raw = snapshot()
    event = raw['events'][0]
    m = event['markets'][0]
    if change == 'mixed':
        v2 = snapshot('v2')
        event['markets'][0] = v2['events'][0]['markets'][0]
        raw['books'].update(v2['books'])
    elif change == 'wrong-group':
        m['negRiskMarketID'] = 'different'
    elif change == 'event-group':
        event['negRiskMarketID'] = 'different'
    elif change == 'single':
        event['markets'] = [m]
    elif change == 'augmented':
        event['negRiskAugmented'] = True
    elif change == 'duplicate':
        event['markets'].append(copy.deepcopy(m))
    else:
        m['negRiskOther' if change == 'other' else 'negRiskPlaceholder'] = True
    report = scan(raw)
    group = report['groups'][0]
    assert not group['complete'] and group['conversion_ratio'] is None
    assert group['yes_ask_sum'] is None and group['skipped_reasons']
    assert any(e.get('stage') == 'group' for e in report['errors'])
    assert all(r['kind'] == 'buy_binary_merge' for r in report['opportunities'])


def test_v2_offline_cli_and_deep_scanner_keep_identity(tmp_path):
    raw = snapshot('v2')
    raw.update(selected_event_id='1', resolution_checked_at=raw['as_of'])
    raw['resolutions'] = {m['id']: {'data': [dict(condition_id=m['conditionId'], status='posed',
        was_disputed=False, extended_review=False)]} for m in raw['events'][0]['markets']}
    deep_raw = dict(snapshots=[raw], started_at=raw['as_of'], finished_at=raw['as_of'])
    with patch('socket.socket', side_effect=AssertionError('network forbidden')):
        report = deep_scan(deep_raw)
        output = tmp_path / 'report.json'
        assert main(['--fixture', str(FIXTURES / 'negrisk-v2-contract.json'), '--json', str(output)]) == 0
    assert report['groups'][0]['complete']
    assert report['groups'][0]['markets'][0]['asset_selection']['protocol_version'] == 'v2'
    assert all(leg['asset_kind'] == 'position' for c in report['groups'][0]['candidates'] for leg in c.get('legs', []))
    assert 'position' in output.read_text()
    raw['events'][0]['markets'][0]['module'] = 'combo'
    rejected = deep_scan(deep_raw)['groups'][0]
    assert rejected['skipped_markets'][0]['reason'] == 'unsupported_combo_module'
    assert all(c['kind'] == 'buy_binary_merge' for c in rejected['candidates'])

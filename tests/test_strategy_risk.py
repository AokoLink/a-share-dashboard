import numpy as np
import pandas as pd
import pytest

from core import strategy_risk, strategies, screening_engine, research_evaluation, execution_evidence
from core import daily_data, signal_replay
from core.stage1_data import _write_json
from tests.test_strategies import frame
from tests.test_account_ledger import inputs, run, fills, DAYS, CODES
from tests.test_candidate_recovery import client, DAY


def ordinary():
    return frame([10.] * 65 + [10.8], [100000] * 61 + [200000] * 5,
                 [10.1] * 65 + [10.9]).assign(turnover=.02)


@pytest.mark.parametrize('last', [
    [7.65, 8.42, 9.26, 10.19, 11.21, 12.33, 11.90, 13.09, 13.40, 13.54, 14.72],
    [9.91, 10.90, 11.99, 13.19, 14.51, 15.96, 17.25, 17.40, 17.92, 17.88, 19.52],
])
def test_rapid_runup_rejected_using_only_friday_bars_for_arbitrary_code(last):
    data = frame([last[0]] * 65 + last).assign(turnover=.02)
    result = strategies.screen(data, '600777', '任意股票')
    assert not result['eligible'] and not result['signals']
    risk = strategy_risk.assess(data, '600777')
    assert 'rapid_gain_10d' in risk['reasons']
    assert 'extended_ma20' in risk['reasons']


def test_ordinary_breakout_survives_with_visible_risk_and_nonprobability_score():
    result = strategies.screen(ordinary(), '600001', '测试')['signals'][0]
    assert result['strategy'] == 'breakout'
    assert not result['risk']['blocked']
    assert result['score_basis'] == 'technical_match_not_probability'
    assert result['announcement_status'] == 'unverified'


def test_hard_risk_precedes_pool_cutoff_and_future_cannot_change_it():
    safe = ordinary()
    hot = ordinary().assign(turnover=.3, amount=1e12)
    day = str(safe['date'].iloc[-1].date())
    rows = [{'code': '600001', 'name': '高风险'}, {'code': '600002', 'name': '正常'}]
    result = screening_engine.select(day, {'600001': hot, '600002': safe}, rows, pool_n=1)
    assert result['pool'][0]['code'] == '600002'
    assert result['eligibility']['600001']['reason'] == 'extreme_turnover'
    future = hot.iloc[-1:].copy()
    future['date'] += pd.Timedelta(days=7)
    future['close'] = 1
    future['turnover'] = 0
    changed = screening_engine.select(day, {'600001': pd.concat([hot, future]), '600002': safe}, rows, pool_n=1)
    assert result['decision_sha256'] == changed['decision_sha256']


def test_penalty_reduces_score_without_being_a_probability():
    low = strategies.screen(ordinary(), '600001')['signals'][0]
    high = strategies.screen(ordinary().assign(turnover=.20), '600001')['signals'][0]
    assert high['raw_score'] == low['raw_score']
    assert high['score'] < low['score'] and high['risk_penalty'] > 0


def test_board_limits_and_missing_turnover_are_explicit():
    data = frame([10.] * 65 + [13.]).assign(turnover=.02)
    assert strategy_risk.assess(data, '600001')['blocked']
    assert not strategy_risk.assess(data, 'sz300001')['blocked']
    risk = strategy_risk.assess(ordinary().assign(turnover=np.nan), '600001')
    assert risk['metrics']['turnover'] is None and 'turnover_unverified' in risk['caveats']
    assert strategy_risk.assess(ordinary().assign(volume=np.nan), '600001')['blocked']


@pytest.mark.parametrize('close,opened,expected', [(14.72, 13.25, False), (19.52, 17.57, False),
    (10., 9.7, False), (10., 9.70001, True), (10., 10.5, True), (10., 10.5001, False),
    (-10., -10., False), (float('nan'), 10., False)])
def test_gap_boundaries(close, opened, expected):
    assert strategies.execution_check(close, opened)[0] is expected


def test_lower_gap_cancelled_slot_stays_cash_in_accounts_and_research():
    data = inputs()
    data['bars'][CODES[0]][DAYS[1]]['open'] = 9.
    report = run(data)
    assert not fills(report, 'buy') and report['cash'] == 100000.
    assert report['orders'][0]['reason'] == 'entry_gap_below_limit'
    prices = pd.DataFrame([{'date': day, **row} for day, row in data['bars'][CODES[0]].items()])
    result = research_evaluation.execution_record(CODES[0], {CODES[0]: prices}, DAYS[0], DAYS, 2)
    assert result['status'] == 'cancelled_gap' and result['net_return'] is None
    arm = research_evaluation.arm([result])
    assert arm['data_complete'] and arm['fixed_weight_net_proxy'] == 0
    assert arm['conditional_net'] is None


@pytest.mark.parametrize('change', ['missing', 'stale', 'future', 'incomplete', 'risk'])
def test_announcement_review_blocks_unverified_or_adverse_entry(change):
    data = inputs()
    row = data['announcement_reviews'][DAYS[1]][CODES[0]]
    if change == 'missing':
        data['announcement_reviews'] = {}
    elif change == 'stale':
        row['reviewed_through'] = DAYS[0] + 'T15:00:00+08:00'
    elif change == 'future':
        row['known_at'] = DAYS[1] + 'T10:00:00+08:00'
    elif change == 'incomplete':
        row['complete'] = False
    else:
        row['risk_flags'] = ['severe_abnormal_trading']
    assert not fills(run(data, mode='verified_inputs'), 'buy')
    if change == 'risk':
        assert not fills(run(data), 'buy')
    else:
        proxy = run(data)
        assert not proxy['formal']
        assert any(reason == 'announcement_review_unknown' for day in proxy['daily'] for _, reason in day['evidence_limits'])


def test_announcement_import_requires_source_and_valid_cutoff(tmp_path):
    evidence = signal_replay.immutable(tmp_path / 'source.json', {'official_announcement': 'fixture'})
    row = {'code': '600001', 'date': DAYS[1], 'verified': True, 'source': 'fixture',
        'known_at': DAYS[1] + 'T09:15:00+08:00', 'reviewed_through': DAYS[1] + 'T09:10:00+08:00',
        'complete': True, 'risk_flags': [], 'evidence_refs': [evidence]}
    execution_evidence.import_bundle(tmp_path, {'announcement_reviews': [row]}, now=DAYS[2])
    assert execution_evidence.load(tmp_path)[0]['announcement_reviews'][0]['risk_flags'] == []
    row['reviewed_through'] = DAYS[1] + 'T10:00:00+08:00'
    with pytest.raises(ValueError, match='review_time'):
        execution_evidence.import_bundle(tmp_path, {'announcement_reviews': [row]}, now=DAYS[2])


def test_legacy_api_excludes_hot_candidates_preserves_archive_and_missing_proof(tmp_path, monkeypatch):
    data = ordinary().assign(turnover=.30)
    data['date'] = pd.bdate_range(end=DAY, periods=len(data)).strftime('%Y-%m-%d')
    artifact = daily_data.archive_frame(tmp_path / 'prices.csv', data, 'fixture', DAY)
    inputs_ref = signal_replay.immutable(tmp_path / 'inputs.json', {'prices': {'600001': {
        'status': 'success', 'artifact': artifact, 'response': artifact}}})
    report = {'version': 'old', 'data_as_of': DAY, 'frozen_version': 'old', 'groups': {'breakout': [
        {'code': 'sh600001', 'name': '测试', 'score': 90}, {'code': 'sh600002', 'name': '缺数据', 'score': 80}]},
        'input_manifest': inputs_ref}
    path = tmp_path / 'candidates' / (DAY + '.json')
    _write_json(path, report)
    original = path.read_bytes()
    http = client(tmp_path, monkeypatch)
    original_view = http.get('/api/strategy-candidates?date=' + DAY).json['data']
    assert [r['code'] for r in original_view['groups']['breakout']] == ['sh600001', 'sh600002']
    assert [r['score'] for r in original_view['groups']['breakout']] == [90, 80]
    out = http.get('/api/strategy-candidates?view=revised&date=' + DAY).json['data']
    assert not out['groups']['breakout']
    assert out['risk_review']['blocked'][0]['code'] == 'sh600001'
    assert out['risk_review']['unknown'][0]['code'] == 'sh600002'
    assert out['view'] == 'revised' and out['snapshot_ids'] == {} and out['forward_signal'] is False
    assert path.read_bytes() == original

"""Partial spatial intervals cannot be shown as full population coverage."""
import copy
import sys
from pathlib import Path
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
from sps.reporting import build_reports, _coverage_rows
from sps.contract import JobError
from test_reporting_b import report_case

def partial():
    return {'nominal_coverage':.8,'repeat':0,'fold':0,'status':'partial','n':20,'group_count':10,
            'available_n':12,'unavailable_n':8,'available_fraction':.6,'available_group_count':6,
            'reason_code':'PARTIAL_SPATIAL_SUPPORT','reason':'Local support is insufficient for eight observations.',
            'available_subset':{'coverage':.75,'mean_width':2.,'median_width':2.,'interval_score':3.},
            'metrics_scope':'available_intervals_only'}

def test_partial_coverage_report_labels_both_denominators(tmp_path,report_case):
    ev,samples,settings=report_case
    ev=copy.deepcopy(ev)
    ev['coverage_diagnostics']={'outer_folds':[partial()]}
    ev['mapping_diagnostics']={'interval_support':{'valid_input_pixels':100,'available_input_pixels':60,'unavailable_input_pixels':40,'available_fraction':.6}}
    for row in ev.get('oof',[])+ev.get('independent_predictions',[]):
        if 'lower' in row:row['interval_status']='available'
    settings['uncertainty']={'method':'spatial_conformal','coverage':.8}
    build_reports(tmp_path/'report',ev,samples,settings,check_cancel=lambda:None)
    html=(tmp_path/'report/report.html').read_text(encoding='utf-8')
    assert 'Only observations with a computed interval' in html
    assert 'Cells without local support' in html
    assert '12</td><td>8</td>' in html
    assert '0,75' in html or '0.75' in html
    assert _coverage_rows(ev)[0].get('coverage') is None

@pytest.mark.parametrize('bad',[
    {'coverage':.75}, {'available_n':20}, {'unavailable_n':1},
    {'available_fraction':.8}, {'available_subset':{'coverage':2}}])
def test_partial_coverage_inconsistency_is_rejected(bad):
    row=partial();row.update(bad)
    with pytest.raises(JobError) as error:_coverage_rows({'coverage_diagnostics':{'outer_folds':[row]}})
    assert error.value.code=='REPORT_COVERAGE'

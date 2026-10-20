"""Calendar order and indivisible profiles for forward evaluation."""
from datetime import date, timedelta
import json
from pathlib import Path
import sys
import numpy as np
import pytest
from pyproj import CRS
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
from sps.contract import JobError
from sps.temporal_d import normalize_dates, temporal_periods
from sps.validation import make_folds, split_calibration
from sps.engine_b import write_folds

def data():
    ids=['s%02d'%i for i in range(48)]
    xy=np.column_stack([np.arange(48)*100+400000,np.repeat(4200000.,48)])
    groups=np.asarray(ids)
    dates={v:(date(2020,1,1)+timedelta(days=(i//2)*3)).isoformat() for i,v in enumerate(ids)}
    cfg=dict(method='temporal',n_splits=3,inner_splits=2,repeats=1,seed=4,
             temporal_mode='temporal_new_groups',timezone='UTC',gap_days=1,
             initial_train_periods=8,inner_initial_train_periods=2,dates_by_id=dates)
    return ids,xy,groups,cfg

def test_temporal_outer_inner_order_and_stability():
    ids,xy,g,c=data(); folds=make_folds(ids,xy,g,c,crs='EPSG:32637')
    reverse=np.arange(len(ids))[::-1]
    assert folds==make_folds([ids[i] for i in reverse],xy[reverse],g[reverse],c,crs='EPSG:32637')
    tested=[v for f in folds for v in f['test_ids']]
    assert len(tested)==32 and len(set(tested))==32
    assert set(ids[:16]).isdisjoint(tested)
    for f in folds:
        assert max(c['dates_by_id'][v] for v in f['train_ids'])<min(c['dates_by_id'][v] for v in f['test_ids'])
        idx=[ids.index(v) for v in f['train_ids']]
        for inner in make_folds(f['train_ids'],xy[idx],g[idx],c,crs='EPSG:32637',inner=True):
            assert set(inner['test_ids'])<=set(f['train_ids'])
            assert max(c['dates_by_id'][v] for v in inner['train_ids'])<min(c['dates_by_id'][v] for v in inner['test_ids'])
        assert f['diagnostics']['temporal']['gap_days']==1

def test_calendar_gap_and_latest_calibration():
    ids,xy,g,c=data(); c['gap_days']=4
    train,cal=split_calibration(ids,xy,g,c,.25,crs='EPSG:32637',seed=1)
    assert set(cal)==set(ids[-12:])
    a=max(date.fromisoformat(c['dates_by_id'][v]) for v in train)
    b=min(date.fromisoformat(c['dates_by_id'][v]) for v in cal)
    assert a+timedelta(days=4)<b
    assert set(ids)-set(train)-set(cal)==set(ids[34:36])
    assert (train,cal)==split_calibration(ids,xy,g,c,.25,crs='EPSG:32637',seed=999)

def test_overlapping_profile_dates_merge_transitively():
    ids=['a','b','c','d','e'];g=['p','q','p','q','r']
    dates=dict(zip(ids,['2020-01-01','2020-01-02','2020-01-03','2020-01-04','2020-01-05']))
    p=temporal_periods(ids,g,dates)
    assert p[0]['ids']==['a','b','c','d'] and p[1]['ids']==['e']

def test_same_day_stays_in_one_role():
    ids,xy,g,c=data();folds=make_folds(ids,xy,g,c,crs='EPSG:32637')
    for f in folds:
        assert not {c['dates_by_id'][v] for v in f['train_ids']} & {c['dates_by_id'][v] for v in f['test_ids']}

@pytest.mark.parametrize('value',[None,'','01/02/2020','2020-02-30','2020-1-2',42,'2020-02-01junk'])
def test_ambiguous_or_missing_date_rejected(value):
    with pytest.raises(JobError,match='a') as e:normalize_dates(['a'],[{'d':value}],'d','UTC')
    assert e.value.code=='TEMPORAL_DATE'

def test_leap_day_and_timezone_conversion():
    dates=normalize_dates(['a','b'],[{'d':'2020-02-29'},{'d':'2020-02-29T23:30:00-02:00'}],'d','UTC')
    assert dates=={'a':'2020-02-29','b':'2020-03-01'}

@pytest.mark.parametrize('value',['2024-03-10T02:30:00','2024-11-03T01:30:00'])
def test_ambiguous_and_nonexistent_local_time_rejected(value):
    with pytest.raises(JobError) as e:normalize_dates(['a'],[{'d':value}],'d','America/New_York')
    assert e.value.code=='TEMPORAL_DATE'

def test_independent_must_be_future():
    ids,xy,g,c=data();c['independent_ids']=ids[-2:]
    folds=make_folds(ids,xy,g,c,crs='EPSG:32637')
    assert all(not set(c['independent_ids']) & (set(f['train_ids'])|set(f['test_ids'])) for f in folds)
    c['independent_ids']=ids[:2]
    with pytest.raises(JobError) as e:make_folds(ids,xy,g,c,crs='EPSG:32637')
    assert e.value.code=='TEMPORAL_ORDER'

@pytest.mark.parametrize('change',[{'repeats':2},{'temporal_mode':'forecast_same_groups'},{'initial_train_periods':24},{'gap_days':1000}])
def test_insufficient_or_unsupported_design_rejected(change):
    ids,xy,g,c=data();c.update(change)
    with pytest.raises(JobError):make_folds(ids,xy,g,c,crs='EPSG:32637')

def test_initial_history_is_not_independent(tmp_path):
    ids,xy,g,c=data();c['independent_ids']=ids[-2:];f=make_folds(ids,xy,g,c,crs='EPSG:32637')
    p=tmp_path/'folds.geojson'
    write_folds(p,f,{'ids':ids,'coordinates':xy,'crs':CRS.from_epsg(32637)},g,independent_ids=c['independent_ids'])
    rows=[v['properties'] for v in json.loads(p.read_text())['features']]
    assert {v['sample_id'] for v in rows if v['role']=='independent'}==set(ids[-2:])
    assert {v['sample_id'] for v in rows if v['role']=='initial_training'}==set(ids[:16])

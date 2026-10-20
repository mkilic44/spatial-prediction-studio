from datetime import datetime, timezone
from pathlib import Path
import sys
import numpy as np
from pyproj import CRS

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
from sps.study_context import numeric_summary, build_study_context


def test_description_missing_and_single_sample_standard_deviation():
    result=numeric_summary([np.nan, 4])
    assert result['missing_n']==1 and result['mean']==4
    assert result['std'] is None and result['std_reason']=='fewer_than_two_observations'
    assert numeric_summary([])['status']=='unavailable'


def test_descriptive_roles_and_categories_are_kept_separate():
    ids=['a','b','c','d']
    samples={'ids':ids, 'y':np.array([1.,3.,100.,200.]),'crs':CRS.from_epsg(32637), 'depth':None,'laboratory_method':None,'support':'point'}
    covariates=[{'id':'cont','unit':'m','kind':'continuous'}, {'id':'class','unit':'1','kind':'categorical'}]
    settings={'samples':{'target_field':'soil','unit':'g/kg'},'covariates':covariates}
    ev={'final_record':{'development_ids':['a','b'],'calibration_ids':['c'],'independent_ids':['d'],'model':{'id':'ridge'}}, 'oof':[{'sample_id':'a'},{'sample_id':'b'},{'sample_id':'a'}]}
    result=build_study_context(np.array([[1,1],[2,2],[np.nan,2],[50,3]]), samples, ['g1','g1','g2','g3'],settings,ev,[],datetime.now(timezone.utc).isoformat(),
                               {'path':'model.joblib','metadata_path':'model.joblib.meta.json','sha256':'x'}, check_cancel=lambda:None)
    rows=result['descriptive_statistics']
    target=next(r for r in rows if r['scope']=='final_training' and r['role']=='target')
    assert target['mean']==2 and target['unique_groups']==1 and target['unique_samples']==2
    held=next(r for r in rows if r['scope']=='independent_test' and r['role']=='target')
    assert held['mean']==200
    cat=next(r for r in rows if r['scope']=='final_training' and r['variable']=='class')
    assert cat['categories']==[{'value':1.,'count':1},{'value':2.,'count':1}]
    assert 'mean' not in cat
    outer=next(r for r in rows if r['scope']=='outer_tested_unique' and r['role']=='target')
    assert outer['n']==2
    assert result['model_artifact']['roundtrip_verified'] is True

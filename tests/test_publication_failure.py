import pytest
from pathlib import Path
from test_engine_b import request,run_request
from sps.contract import Cancelled,JobError


@pytest.mark.parametrize('failure',[Cancelled(),JobError('PACKAGE_TEST','Packaging test')])
def test_late_package_failure_invalidates_report_and_preserves_failed_record(tmp_path,monkeypatch,failure):
    import sps.model_bundle
    def fail(*args,**kwargs):
        assert (tmp_path/'run/study_report.html').is_file()
        assert (tmp_path/'run/study_report_index.html').is_file()
        assert list((tmp_path/'run').glob('study_report_page_*.html'))
        raise failure
    monkeypatch.setattr(sps.model_bundle,'create_model_package',fail)
    result,_=run_request(request(tmp_path),tmp_path)
    assert result['status']==('cancelled' if isinstance(failure,Cancelled) else 'failed')
    assert result['outputs']==[]
    assert not (tmp_path/'run/study_report.html').exists()
    assert not list((tmp_path/'run').glob('study_report*.html'))
    assert not (tmp_path/'run/report.html').exists()
    assert not list((tmp_path/'run').glob('*.joblib'))
    assert (tmp_path/'run/INCOMPLETE.json').is_file()

"""Forward evaluation on new groups; dates never depend on target values."""
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import numpy as np
from .contract import JobError


def normalize_dates(ids, props, date_field, timezone_name):
    if not isinstance(date_field,str) or not date_field.strip():
        raise JobError('TEMPORAL_DATE','Zaman sıralı doğrulama için tarih alanı seçilmelidir.')
    try:
        tz=ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError,TypeError,ValueError) as exc:
        raise JobError('TEMPORAL_DATE','Saat dilimi bulunamadı: '+str(timezone_name)) from exc
    if len(ids)!=len(props) or len(set(ids))!=len(ids):
        raise JobError('TEMPORAL_DATE','Tarih özellikleri gözlem kimlikleriyle eşleşmiyor.')
    result={}
    for identity,row in zip(ids,props):
        value=row.get(date_field)
        try:
            if not isinstance(value,str):raise ValueError('text required')
            if re.fullmatch(r'\d{4}-\d{2}-\d{2}',value):
                day=date.fromisoformat(value)
            else:
                if not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})?',value):
                    raise ValueError('ISO date/time required')
                timestamp=datetime.fromisoformat(value)
                if timestamp.tzinfo is None:
                    candidates=[timestamp.replace(tzinfo=tz,fold=f) for f in (0,1)]
                    valid=[v for v in candidates if v.astimezone(timezone.utc).astimezone(tz).replace(tzinfo=None)==timestamp]
                    if not valid or len({v.utcoffset() for v in valid})!=1:
                        raise ValueError('ambiguous or nonexistent local time')
                    timestamp=valid[0]
                day=timestamp.astimezone(tz).date()
            result[identity]=day.isoformat()
        except (ValueError,TypeError,OverflowError) as exc:
            raise JobError('TEMPORAL_DATE','Tarih eksik, belirsiz veya geçersiz: '+str(identity)+' / '+date_field) from exc
    return result


def temporal_periods(ids,groups,dates_by_id):
    """Merge overlapping group date ranges transitively, including same days."""
    if not isinstance(dates_by_id,dict) or len(ids)!=len(groups):
        raise JobError('TEMPORAL_DATE','Kimliklerle eşleşen tarih kaydı gereklidir.')
    by_group=defaultdict(list); dates={}
    for identity,group in zip(ids,groups):
        try:
            value=dates_by_id[identity]
            if not isinstance(value,str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}',value):raise ValueError()
            dates[identity]=date.fromisoformat(value)
        except (KeyError,ValueError,TypeError) as exc:
            raise JobError('TEMPORAL_DATE','Geçerli tarih bulunamadı: '+str(identity)) from exc
        by_group[str(group)].append(identity)
    spans=sorted((min(dates[i] for i in members),max(dates[i] for i in members),group,sorted(members))
                 for group,members in by_group.items())
    periods=[]
    for start,end,group,members in spans:
        if periods and start<=periods[-1]['end_date']:
            p=periods[-1];p['end_date']=max(p['end_date'],end)
            p['ids'].extend(members);p['group_ids'].append(group)
        else:
            periods.append(dict(start_date=start,end_date=end,ids=list(members),group_ids=[group]))
    for p in periods:
        p['ids'].sort();p['group_ids'].sort()
    return periods


def _settings(config,inner=False):
    if config.get('temporal_mode','temporal_new_groups')!='temporal_new_groups' or config.get('repeats',1)!=1:
        raise JobError('TEMPORAL_MODE','İlk sürüm yalnız yeni gruplarda tek tekrarlı zaman sıralı doğrulama içerir.')
    gap=config.get('gap_days',0)
    initial=config.get('inner_initial_train_periods' if inner else 'initial_train_periods',2 if inner else 8)
    if type(gap)!=int or gap<0 or type(initial)!=int or initial<1:
        raise JobError('TEMPORAL_PERIODS','Takvim boşluğu ve ilk eğitim dönemi sayısı geçersiz.')
    return gap,initial


def temporal_folds(ids,groups,dev,metric,metric_crs,config,*,inner,n_splits,seed,minimum,buffer,check_cancel):
    from .validation import _fold,_buffer_training
    gap,initial=_settings(config,inner)
    periods=temporal_periods([ids[i] for i in dev],groups[dev],config.get('dates_by_id'))
    if len(periods)<initial+n_splits:
        raise JobError('TEMPORAL_PERIODS','Gruplar ve aynı günler birleştirildikten sonra ilk eğitim ve test pencereleri için yeterli dönem kalmadı.')
    held=[i for i in range(len(ids)) if i not in set(dev)]
    if held:
        independent=temporal_periods([ids[i] for i in held],groups[held],config.get('dates_by_id'))
        if (independent[0]['start_date']-periods[-1]['end_date']).days<=gap:
            raise JobError('TEMPORAL_ORDER','Bağımsız test, bütün geliştirme tarihlerinden ve takvim boşluğundan sonra başlamalıdır.')
    lookup={identity:i for i,identity in enumerate(ids)};folds=[]
    for fold,window in enumerate(np.array_split(np.arange(initial,len(periods)),n_splits)):
        check_cancel();first=periods[int(window[0])]['start_date'];last=periods[int(window[-1])]['end_date']
        preceding=[p for p in periods[:int(window[0])] if (first-p['end_date']).days>gap]
        train=np.asarray(sorted(lookup[i] for p in preceding for i in p['ids']),dtype=int)
        test=np.asarray(sorted(lookup[i] for j in window for i in periods[int(j)]['ids']),dtype=int)
        before=set(train);train=_buffer_training(train,test,groups,metric,buffer)
        item=_fold(ids,groups,metric,metric_crs,dev,train,test,0,fold,seed,'temporal',minimum,buffer)
        dates=config['dates_by_id']
        item['diagnostics']['temporal']={
            'timezone':config.get('timezone','UTC'),'gap_days':gap,'initial_train_periods':initial,
            'train_start':min(dates[ids[i]] for i in train),'train_end':max(dates[ids[i]] for i in train),
            'test_start':first.isoformat(),'test_end':last.isoformat(),'test_calendar_days':(last-first).days+1,
            'train_periods':len({j for j,p in enumerate(periods) if any(lookup[i] in set(train) for i in p['ids'])}),
            'test_periods':len(window),
            'future_ids':sorted(i for p in periods[int(window[-1])+1:] for i in p['ids']),
            'gap_ids':sorted(i for p in periods[:int(window[0])] if p not in preceding for i in p['ids']),
            'buffer_ids':sorted(ids[i] for i in before-set(train))}
        folds.append(item)
    return folds


def temporal_calibration(ids,groups,dev,metric,config,fraction,minimum,buffer):
    from .validation import _buffer_training
    import math
    gap,_=_settings(config,True)
    periods=temporal_periods([ids[i] for i in dev],groups[dev],config.get('dates_by_id'))
    needed=math.ceil(len(set(groups[dev]))*fraction);count=0;chosen=[]
    for p in reversed(periods):
        chosen.append(p);count+=len(p['group_ids'])
        if count>=needed:break
    if not chosen or len(chosen)>=len(periods):
        raise JobError('TEMPORAL_CALIBRATION','Son dönemler ayrıldıktan sonra geliştirme geçmişi kalmadı.')
    start=chosen[-1]['start_date'];lookup={identity:i for i,identity in enumerate(ids)}
    calibration=np.asarray(sorted(lookup[i] for p in chosen for i in p['ids']),dtype=int)
    training=np.asarray(sorted(lookup[i] for p in periods if (start-p['end_date']).days>gap for i in p['ids']),dtype=int)
    training=_buffer_training(training,calibration,groups,metric,buffer)
    if len(set(groups[training]))<minimum:
        raise JobError('TEMPORAL_CALIBRATION','Takvim ve mekânsal boşluk sonrasında yeterli geliştirme grubu kalmadı.')
    return sorted(ids[i] for i in training),sorted(ids[i] for i in calibration)

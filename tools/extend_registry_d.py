"""Add experimental D schemas without resetting accepted A-C methods."""
import json
from copy import deepcopy
from pathlib import Path
from make_registry import p, enum

ROOT=Path(__file__).resolve().parents[1]
path=ROOT/'backend/sps/registry.json'
obj=json.loads(path.read_text(encoding='utf-8'))
common={'n_epochs':p('Eğitim turu','int',10,min=1,max=200),
        'batch_size':p('Eğitim yığını','int',16,min=4,max=512),
        'hidden_size':p('Gizli katman genişliği','int',32,min=8,max=256),
        'n_layers':p('Gizli katman sayısı','int',2,min=1,max=4)}
schemas={
 'tabm':{**common,'k':p('Topluluk üyesi','int',4,min=2,max=32),
         'dropout':p('Eğitimde birim kapatma oranı','float',.1,min=0,max=.8),
         'lr':p('Öğrenme oranı','float',.001,min=.000001,max=.2),
         'weight_decay':p('Ağırlık cezası','float',.0001,min=0,max=.1)},
 'realmlp':{**common,'lr':p('Öğrenme oranı','float',.04,min=.000001,max=.2)}}
for row in obj['categories']['model']:
    if row['id'] in schemas:
        row.update(parameters=schemas[row['id']],enabled=False,status='implementation_testing',
                   reason='D karşılaştırmalı kabul sınamaları sürüyor.',
                   label=('TabM' if row['id']=='tabm' else 'RealMLP')+' (deneysel)',
                   description='Yerel işlemcide sabit turla eğitim. Gizli doğrulama ve erken durdurma kapalı. Her eğitim en fazla 120 saniye ve 2 GiB bellek kullanabilir.',
                   cost='En fazla 2000 eğitim gözlemi ve kodlama sonrası 256 değişken; tek iş parçacığı. Küçük veride de yavaş olabilir.')
row=next(r for r in obj['categories']['uncertainty'] if r['id']=='spatial_conformal')
row.update(enabled=False,status='implementation_testing',reason='D karşılaştırmalı kabul sınamaları sürüyor.',
           label='Mekânsal ağırlıklı konformal (deneysel)',
           description='Ayrı kalibrasyon artıklarını uzaklıkla ağırlıklandırır. Kesin kapsama güvencesi yoktur. Yerel destek yetersizse nokta tahmini korunur; aralık üretilmez.',
           parameters={'coverage':p('İstenen kapsama oranı','float',.9,min=.5,max=.99),
                       'calibration_fraction':p('Kalibrasyon için ayrılan grup oranı','float',.3,min=.1,max=.5),
                       'spatial_bandwidth_m':p('Önceden belirlenen bant genişliği (m)','float',1000,min=1,max=1000000),
                       'spatial_kernel':enum('Uzaklık ağırlığı','gaussian',[('gaussian','Gauss'),('compact','Sınırlı destek')]),
                       'spatial_min_effective_n':p('Asgari etkin kalibrasyon sayısı','float',5,min=2,max=10000),
                       'spatial_min_groups':p('Asgari etkin kalibrasyon grubu','int',3,min=2,max=10000)})
row=next(r for r in obj['categories']['model'] if r['id']=='tabpfn')
row.update(enabled=False,status='implementation_testing',reason='D karşılaştırmalı kabul sınamaları sürüyor.',
           label='TabPFN v2 (deneysel)',
           description='Önceden eğitilmiş tablo modeli yerel işlemcide çalışır. Built with PriorLabs-TabPFN. Otomatik indirme ve telemetri kapalıdır.',
           cost='En fazla 1000 eğitim gözlemi ve kodlama sonrası 256 değişken. Her alt süreç en fazla 120 saniye ve 2 GiB bellek kullanabilir.',
           parameters={'n_estimators':p('Topluluk üyesi','int',2,min=1,max=4),
                       'softmax_temperature':p('Olasılık sıcaklığı','float',.9,min=.05,max=5)})
base=deepcopy(next(r for r in obj['categories']['validation'] if r['id']=='random')['parameters'])
base.pop('inner_method',None);base.pop('block_size',None)
base['n_splits']['default']=3;base['inner_splits']['default']=2;base['repeats']['max']=1
base.update(temporal_mode=enum('Zamansal değerlendirme amacı','temporal_new_groups',[('temporal_new_groups','Sonraki tarihlerde yeni profil veya parseller')]),
            timezone=p('Saat dilimi (IANA adı)','string','UTC'),
            gap_days=p('Roller arasında boş takvim günü','int',0,min=0,max=36500),
            initial_train_periods=p('İlk dış eğitimde dönem sayısı','int',12,min=1,max=100000),
            inner_initial_train_periods=p('İlk iç eğitimde dönem sayısı','int',4,min=1,max=100000),
            selector_initial_train_periods=p('Ardışık seçicide ilk eğitim dönemi','int',1,min=1,max=100000))
row=next(r for r in obj['categories']['validation'] if r['id']=='temporal')
row.update(enabled=False,status='implementation_testing',reason='D karşılaştırmalı kabul sınamaları sürüyor.',
           label='Zaman sıralı doğrulama (yeni gruplar)',parameters=base,
           description='Tarih alanı zorunludur. Profiller ve aynı günler bölünmez. Eğitim geçmişi ilerler; kalibrasyon son uygun dönemlerden ayrılır. Sabit rasterlerle değerlendirme, belirli bir gelecek tarihin haritasını doğrulamaz.')
obj['registry_version']='0.4.0-dev'
path.write_text(json.dumps(obj,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
print('D schemas prepared; UI methods remain disabled until acceptance.')

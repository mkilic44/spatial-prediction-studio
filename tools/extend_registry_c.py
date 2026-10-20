"""Extend accepted B registry without resetting its verified methods."""
import json
from copy import deepcopy
from pathlib import Path
from make_registry import p,enum
ROOT=Path(__file__).resolve().parents[1]
path=ROOT/'backend/sps/registry.json'
obj=json.loads(path.read_text(encoding='utf-8'));cats=obj['categories']
schemas={
 'lightgbm':{'n_estimators':p('Ağaç sayısı','int',100,min=10,max=2000),'num_leaves':p('Yaprak sayısı','int',31,min=2,max=255),'max_depth':p('Azami derinlik (-1 sınırsız)','int',-1,min=-1,max=50),'learning_rate':p('Öğrenme oranı','float',.1,min=.0001,max=1),'min_child_samples':p('Yaprakta asgari gözlem','int',10,min=1,max=200),'colsample_bytree':p('Değişken oranı','float',1,min=.1,max=1),'reg_alpha':p('Mutlak ceza','float',0,min=0,max=1000),'reg_lambda':p('Karesel ceza','float',0,min=0,max=1000)},
 'catboost':{'iterations':p('Yineleme','int',100,min=10,max=2000),'depth':p('Derinlik','int',6,min=1,max=12),'learning_rate':p('Öğrenme oranı','float',.1,min=.0001,max=1),'l2_leaf_reg':p('Düzenlileştirme','float',3,min=0,max=1000),'random_strength':p('Rastgelelik gücü','float',1,min=0,max=100)},
 'cubist':{'n_committees':p('Komite sayısı','int',1,min=1,max=100),'n_rules':p('Kural sınırı','int',100,min=1,max=500),'neighbors':p('Düzeltme komşusu (0 kapalı)','int',0,min=0,max=9),'unbiased':p('Yanlılık düzeltmesi','bool',False),'extrapolation':p('Dışa taşıma oranı','float',.05,min=0,max=1)}}
kp={'variogram_model':enum('Yarıvaryogram modeli','spherical',[('spherical','Küresel'),('exponential','Üstel'),('gaussian','Gauss')]),'nlags':p('Uzaklık sınıfı sayısı','int',6,min=3,max=30),'weight':p('Kısa uzaklıklara daha fazla ağırlık','bool',False),'n_closest_points':p('Yerel komşu sayısı','int',12,min=3,max=200)}
schemas['ordinary_kriging']=kp
schemas['regression_kriging']={**kp,'base_model':enum('Yalın model','ridge',[('ridge','Ridge'),('random_forest','Rastgele orman')]),'base_alpha':p('Ridge cezası','float',1,min=.000001,max=1000000),'base_trees':p('Orman ağaç sayısı','int',100,min=10,max=2000),'base_depth':p('Orman derinliği','int',10,min=1,max=100)}
for row in cats['model']:
 if row['id'] in schemas:
  row.update(parameters=schemas[row['id']],enabled=False,status='implementation_testing',reason='C bütünleşme ve ArcMap kabul sınamaları sürüyor.')
  row['description']=('Yalnız ilgili eğitim katında yarıvaryogram. Kriging varyansı toplam belirsizlik değildir; eğitimde en fazla 2000 gözlem.' if 'kriging' in row['id'] else 'Yerel işlemci üzerinde sabitlenmiş gerçek paket. Erken durdurma ve modelin kendi çapraz doğrulaması kapalıdır.')
kn=next(r for r in cats['validation'] if r['id']=='knndm')
kn.update(parameters={**deepcopy(cats['validation'][0]['parameters']),'maxp':p('Bir katın azami gözlem oranı','float',.75,min=.1,max=.99),'prediction_samples':p('Alan örnekleme hedefi','int',1000,min=25,max=4000)},enabled=False,status='implementation_testing',reason='C bütünleşme ve ArcMap kabul sınamaları sürüyor.',description='Yerel CAST 1.0.4 ile uzaklık eşleme. Bu sürümde yalnız tekil bağımsız gözlemler, metrik koordinatlar ve en fazla 2000 eğitim noktası desteklenir.')
obj['registry_version']='0.3.0-dev'
path.write_text(json.dumps(obj,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
print('C schemas extended; accepted B status preserved.')

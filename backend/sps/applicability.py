"""CAST 1.0.4 L2 applicability on a documented numeric predictor design.

Sample SD (ddof=1), mean off-diagonal pair distance, CV-constrained nearest
distance, and min(max(DI), Q3+1.5*IQR) follow the pinned reference. This is
environmental representation, never a prediction interval or accuracy guarantee.
"""
import numpy as np
from scipy.spatial.distance import cdist
from scipy.spatial import cKDTree
from .contract import JobError
from .validation import indices_for


class Applicability:
    def fit(self, X, ids, folds, weights=None, check_cancel=lambda:None):
        X=np.asarray(X,dtype=float);ids=list(ids)
        if X.ndim!=2 or len(X)!=len(ids) or len(X)<3 or len(X)>10000 or not np.isfinite(X).all():
            raise JobError('AOA_INPUT','Uygulanabilirlik için 3-10000 sonlu eğitim satırı gerekir.')
        self.center=X.mean(axis=0);self.scale=X.std(axis=0,ddof=1)
        if not len(self.scale) or np.any(self.scale<=1e-12):
            raise JobError('AOA_VARIANCE','Uygulanabilirlik tasarımında sıfır varyans var.')
        self.weights=np.ones(X.shape[1]) if weights is None else np.asarray(weights,dtype=float)
        if self.weights.shape!=(X.shape[1],) or not np.isfinite(self.weights).all() or np.any(self.weights<0) or not np.any(self.weights>0):
            raise JobError('AOA_WEIGHTS','Uygulanabilirlik ağırlıkları sonlu, negatif olmayan ve en az biri pozitif olmalıdır.')
        self.training=(X-self.center)/self.scale*self.weights
        total=0.
        for start in range(0,len(X),256):
            check_cancel();d=cdist(self.training[start:start+256],self.training)
            d[np.arange(len(d)),np.arange(start,start+len(d))]=0
            total+=float(d.sum())
        self.mean_distance=total/(len(X)*(len(X)-1))
        if self.mean_distance<=0:raise JobError('AOA_VARIANCE','Ağırlıklı uzayda sıfır varyans var.')
        nearest=np.full(len(X),np.nan);seen=np.zeros(len(X),dtype=int)
        for fold in folds:
            check_cancel();tr=indices_for(ids,fold['train_ids']);te=indices_for(ids,fold['test_ids'])
            if len(tr)<1 or set(tr)&set(te):raise JobError('AOA_FOLDS','Uygulanabilirlik katları çakışıyor veya eğitim boş.')
            seen[te]+=1;nearest[te]=cKDTree(self.training[tr]).query(self.training[te],k=1)[0]
        if not np.all(seen==1) or not np.isfinite(nearest).all():
            raise JobError('AOA_FOLDS','Her eğitim gözlemi eşik katlarında tam bir kez sınanmalıdır.')
        self.train_di=nearest/self.mean_distance
        q1,q3=np.quantile(self.train_di,[.25,.75],method='linear')
        self.threshold=float(min(self.train_di.max(),q3+1.5*(q3-q1)))
        self.tree=cKDTree(self.training)
        self.record={'reference':'CAST 1.0.4 trainDI/aoa','method':'L2','training_ids':ids,
            'center':self.center.tolist(),'sample_sd':self.scale.tolist(),'weights':self.weights.tolist(),
            'mean_pair_distance':self.mean_distance,'train_di':self.train_di.tolist(),'threshold':self.threshold,
            'threshold_rule':'min(max(DI), Q3 + 1.5 * IQR); R quantile type 7',
            'folds':folds,'scope':'environmental representation; not prediction uncertainty'}
        return self

    def predict(self,X):
        X=np.asarray(X,dtype=float)
        if X.ndim!=2 or X.shape[1]!=len(self.center) or not np.isfinite(X).all():
            raise JobError('AOA_INPUT','Tahminin uygulanabilirlik tasarımı geçersiz.')
        di=self.tree.query((X-self.center)/self.scale*self.weights,k=1)[0]/self.mean_distance
        return di,di<=self.threshold


class ApplicableWorkflow:
    def __init__(self,base,pipeline,columns,applicability):
        self.base=base;self.pipeline=pipeline;self.columns=columns;self.applicability=applicability
        self.requires_coordinates=getattr(base,'requires_coordinates',False)
        self.record={**base.record,'applicability':applicability.record}
    def _call(self,name,X,coordinates=None,*args):
        from .modeling import prediction_call
        return prediction_call(self.base,name,X,coordinates,*args)
    def predict(self,X,coordinates=None):return self._call('predict',X,coordinates)
    def plain_predict(self,X,coordinates=None):return self._call('plain_predict',X,coordinates)
    def predict_interval(self,X,coordinates=None):return self._call('predict_interval',X,coordinates)
    def predict_interval_at_coverage(self,X,coverage,coordinates=None):return self._call('predict_interval_at_coverage',X,coordinates,coverage)
    def quantile_crossings(self,X,coordinates=None):return self._call('quantile_crossings',X,coordinates)
    def quantile_crossings_at_coverage(self,X,coverage,coordinates=None):return self._call('quantile_crossings_at_coverage',X,coordinates,coverage)
    def diagnostics(self,X,coordinates=None):
        diag=self._call('diagnostics',X,coordinates)
        di,mask=self.applicability.predict(self.pipeline.transform(X)[:,self.columns])
        diag['dissimilarity']=di
        diag['applicability']=mask & ~np.asarray(diag['unknown_category'],dtype=bool)
        return diag


def attach_applicability(model,X,ids,folds,check_cancel):
    base=model
    while not hasattr(base,'pipeline'):base=base.base
    pipeline=base.pipeline;design=pipeline.transform(X)
    columns=np.flatnonzero(design.std(axis=0,ddof=1)>1e-12)
    app=Applicability().fit(design[:,columns],ids,folds,check_cancel=check_cancel)
    app.record.update(design='selected preprocessed numeric design; PCA components if selected; equal weights',
        retained_design_columns=columns.tolist(),removed_constant_columns=np.setdiff1d(np.arange(design.shape[1]),columns).tolist(),
        unknown_category_policy='flag outside applicability in addition to the numeric CAST mask')
    return ApplicableWorkflow(model,pipeline,columns,app)

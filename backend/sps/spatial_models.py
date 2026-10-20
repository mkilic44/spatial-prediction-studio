"""Fold-local ordinary kriging and controlled residual-kriging comparisons."""
from copy import deepcopy
import numpy as np
from .contract import JobError
from .pipeline import audit
from .validation import _metric_coordinates


class KrigingSurface:
    def __init__(self, parameters):
        self.parameters = deepcopy(parameters)

    def fit(self, xy, values):
        xy, values = np.asarray(xy, dtype=float), np.asarray(values, dtype=float)
        if xy.shape != (len(values), 2) or not np.isfinite(xy).all() or not np.isfinite(values).all():
            raise JobError('KRIGING_INPUT', 'Kriging requires a finite target and two coordinates.')
        if len(values) > 2000:
            raise JobError('KRIGING_MEMORY', 'In this version, the dense kriging system accepts at most 2000 training observations.')
        unique, inverse = np.unique(xy, axis=0, return_inverse=True)
        values = np.bincount(inverse, weights=values) / np.bincount(inverse)
        if len(unique) < 6:
            raise JobError('KRIGING_SAMPLE_SIZE', 'The semivariogram requires at least six distinct training locations.')
        self.n = len(unique)
        self.constant = float(values.mean()) if np.ptp(values) <= 1e-12 else None
        self.record = {'unique_training_locations': self.n, 'duplicate_policy': 'mean_within_training_only',
                       'constant_field': self.constant is not None, 'variance_product': 'not_exported_as_total_prediction_uncertainty'}
        if self.constant is not None:
            return self
        from pykrige.ok import OrdinaryKriging
        self.model = OrdinaryKriging(unique[:,0], unique[:,1], values,
            variogram_model=self.parameters.get('variogram_model','spherical'),
            nlags=self.parameters.get('nlags',6), weight=self.parameters.get('weight',False),
            coordinates_type='euclidean', exact_values=True, pseudo_inv=True,
            verbose=False, enable_plotting=False, enable_statistics=False)
        if not np.isfinite(self.model.variogram_model_parameters).all():
            raise JobError('VARIOGRAM', 'The semivariogram fit did not produce finite parameters.')
        self.record.update(variogram_model=self.model.variogram_model,
            parameters=self.model.variogram_model_parameters.tolist(),
            lag_distance_m=self.model.lags.tolist(), semivariance=self.model.semivariance.tolist(),
            moving_neighborhood=min(self.n,self.parameters.get('n_closest_points',12)))
        return self

    def predict(self, xy):
        xy = np.asarray(xy,dtype=float)
        if xy.ndim != 2 or xy.shape[1] != 2 or not np.isfinite(xy).all():
            raise JobError('KRIGING_COORDINATES', 'Prediction coordinates must be two finite columns.')
        if self.constant is not None:
            return np.full(len(xy),self.constant)
        result = np.empty(len(xy), dtype=float)
        for start in range(0,len(xy),512):
            block=xy[start:start+512]
            prediction,_=self.model.execute('points',block[:,0],block[:,1],backend='loop',
                n_closest_points=min(self.n,self.parameters.get('n_closest_points',12)))
            result[start:start+len(block)] = np.asarray(prediction)
        if not np.isfinite(result).all():
            raise JobError('KRIGING_PREDICTION','Kriging did not produce finite predictions.')
        return result


class SpatialWorkflow:
    requires_coordinates = True
    def __init__(self, base, surface, settings, crs, record):
        self.base, self.surface = base, surface
        self.pipeline = base.pipeline
        self.settings, self.crs, self.record = deepcopy(settings), crs, record

    def _coordinates(self, coordinates, n):
        if coordinates is None or np.asarray(coordinates).shape != (n,2):
            raise JobError('KRIGING_COORDINATES','Spatial prediction requires coordinates for every row.')
        return _metric_coordinates(np.asarray(coordinates),self.crs,self.settings['validation'],True)[0]

    def plain_predict(self, X, coordinates=None):
        return self.base.predict(X)

    def predict(self, X, coordinates=None):
        xy=self._coordinates(coordinates,len(X))
        correction=self.surface.predict(xy)
        return correction if self.settings['model']['id']=='ordinary_kriging' else self.base.predict(X)+correction

    def predict_interval(self, X, coordinates=None):
        return None

    def predict_interval_at_coverage(self, X, coverage, coordinates=None):
        raise JobError('NO_INTERVAL_METHOD','Kriging variance is not a total prediction interval; select a separate calibration.')

    def quantile_crossings(self, X, coordinates=None):
        return np.zeros(len(X),dtype=bool)

    def quantile_crossings_at_coverage(self, X, coverage, coordinates=None):
        return np.zeros(len(X),dtype=bool)

    def diagnostics(self, X, coordinates=None):
        result=self.pipeline.diagnostics(X)
        prediction=self.predict(X,coordinates)
        config=self.settings.get('mapping',{})
        result['physical_violation']=np.zeros(len(X),dtype=bool)
        if config.get('physical_min') is not None:result['physical_violation']|=prediction<config['physical_min']
        if config.get('physical_max') is not None:result['physical_violation']|=prediction>config['physical_max']
        result['quantile_crossing']=np.zeros(len(X),dtype=bool)
        return result


def fit_spatial_workflow(X,y,ids,coordinates,groups,covariates,settings,*,crs,role,learning_audit,check_cancel):
    from .modeling import fit_workflow
    check_cancel()
    if settings.get('target_transform',{}).get('method','none')!='none':
        raise JobError('KRIGING_TRANSFORM','In this version, kriging works in the original target unit.')
    if settings.get('uncertainty',{}).get('method','none') not in ('none','split_conformal','spatial_conformal'):
        raise JobError('KRIGING_UNCERTAINTY','For kriging, select no interval or split conformal prediction.')
    if settings.get('feature_selection',{}).get('method') in ('rfe','sfs'):
        raise JobError('KRIGING_SELECTOR','For kriging, the RFE and SFS selectors are turned off because they do not use the inner spatial model.')
    params=settings['model'].get('parameters',{})
    allowed={'variogram_model','nlags','weight','n_closest_points','base_model','base_alpha','base_trees','base_depth'}
    if set(params)-allowed:raise JobError('MODEL_PARAMETER','Unrecognized kriging parameter.')
    xy,_=_metric_coordinates(np.asarray(coordinates),crs,settings['validation'],True)
    plain=deepcopy(settings)
    ordinary=settings['model']['id']=='ordinary_kriging'
    if ordinary and (settings.get('feature_selection',{}).get('method','none')!='none' or settings.get('vif',{}).get('mode','off')!='off'):
        raise JobError('KRIGING_SELECTOR','For ordinary kriging, variable selection and the variance inflation factor diagnostic must be turned off.')
    base_id='mean' if ordinary else params.get('base_model','ridge')
    if base_id not in ('mean','ridge','random_forest'):
        raise JobError('KRIGING_BASE','Unsupported base model.')
    base_params=({'alpha':params.get('base_alpha',1.)} if base_id=='ridge' else
                 {'n_estimators':params.get('base_trees',100),'max_depth':params.get('base_depth',10)} if base_id=='random_forest' else {})
    plain['model']={'id':base_id,'parameters':base_params}
    plain['uncertainty']={'method':'none'}
    base=fit_workflow(X,y,ids,coordinates,groups,covariates,plain,crs=crs,role=role+'/plain',
                      learning_audit=learning_audit,check_cancel=check_cancel)
    residual=np.asarray(y,dtype=float) if ordinary else np.asarray(y,dtype=float)-base.predict(X)
    check_cancel();surface=KrigingSurface(params).fit(xy,residual)
    audit(learning_audit,role,'variogram',ids,model=settings['model']['id'])
    record={**base.record,'model':deepcopy(settings['model']),'settings':deepcopy(settings),
        'uncertainty_method':settings.get('uncertainty',{}).get('method','none'),'kriging':surface.record,
        'residual_definition':'target' if ordinary else 'training_y_minus_in_sample_base_prediction',
        'residual_limitation':'Training residuals can underestimate residual variation when the base model overfits.',
        'covariates_role':'map_mask_only' if ordinary else 'base_regression',
        'plain_comparison_scope':'same fitted base and settings selected for hybrid; controlled residual-component comparison, not independently optimized baseline'}
    return SpatialWorkflow(base,surface,settings,crs,record)

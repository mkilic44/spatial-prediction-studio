"""Write docs/METHODS.md from the method registry, so the guide always matches the software.

Usage: python tools/make_methods_doc.py [--check]
--check exits with status 1 when docs/METHODS.md is out of date.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / 'backend/sps/registry.json'
TARGET = ROOT / 'docs/METHODS.md'

CATEGORIES = [
    ('validation', 'Validation design', '3 Validation',
     'The validation design decides which observations test each model. Outer folds estimate prediction '
     'performance. Inner folds inside each outer training part select variables and tune settings, so the '
     'outer test observations never take part in these choices. Choose the design that matches the '
     'prediction purpose set on the same tab. Observations that share a profile, parcel or farm should be '
     'kept together with a grouped or spatial design. Repeated folds give more evaluation records, but '
     'they do not add independent observations.'),
    ('preprocessing', 'Preprocessing', '4 Preprocessing',
     'Imputation, scaling and the handling of categories are learned only from the training observations '
     'of each fold and then applied to its test observations.'),
    ('target_transform', 'Target transformation', '4 Preprocessing',
     'A transformation is fitted on training data only. Predictions, metrics and maps are reported on the '
     'original target scale. For the log transformation, the mapped quantity (median or mean with a '
     'smearing correction) is fixed before the run.'),
    ('vif', 'Multicollinearity (variance inflation factor)', '5 Variable selection',
     'The variance inflation factor (VIF) measures collinearity among continuous covariates. The diagnostic '
     'mode only reports it. The iterative mode removes covariates above the threshold inside each training '
     'part.'),
    ('feature_selection', 'Variable selection and reduction', '5 Variable selection',
     'Every selection method runs inside the training part of each fold. The selection frequency across '
     'outer-fold models is reported. A comparison with the same model without selection can be requested.'),
    ('model', 'Models', '6 Model and search',
     'The training mean and median ignore the covariates. They give the error level against which the '
     'other models are compared on the same outer folds. Kriging models fit the semivariogram only in the '
     'relevant training fold. Models marked experimental run on the CPU with fixed limits.'),
    ('optimization', 'Hyperparameter search', '6 Model and search',
     'The search runs on the inner folds of each outer training part. The trial budget and the time budget '
     'limit the search. The selected trial belongs to its own inner search.'),
    ('uncertainty', 'Prediction intervals', '8 Uncertainty',
     'A prediction interval is not a confidence interval. Empirical coverage is tested in the outer '
     'evaluation and must be read together with the interval width. Nominal coverage is not an exact '
     'guarantee for spatial or grouped data.'),
]

INSTALL_NOTES = {
    'knndm': 'Needs the optional R 4.4.3 and CAST 1.0.4 runtime, which Install.cmd -WithR installs; without it the run stops with a clear message. See docs/INSTALL.md.',
    'tabpfn': 'Needs the TabPFN v2 weights, which are installed only with Install.cmd -WithTabPFN -AcceptTabPFNLicense.',
}

STATUS = {'backend_verified': 'Available', 'not_implemented': 'Not implemented',
          'implementation_testing': 'Testing', 'acceptance_pending': 'Testing'}


def cell(value):
    return str(value).replace('|', '\\|').replace('\n', ' ')


def number(value):
    if isinstance(value, float) and value.is_integer() and abs(value) < 1e15:
        value = int(value)
    return str(value)


def parameter_rows(parameters):
    rows = []
    for key, spec in parameters.items():
        kind = spec.get('type', '')
        default = spec.get('default', '')
        if kind == 'enum':
            labels = {choice['id']: choice['label'] for choice in spec.get('choices', [])}
            allowed = '; '.join('%s (`%s`)' % (label, ident) for ident, label in labels.items())
            default = labels.get(default, default)
        elif 'min' in spec or 'max' in spec:
            allowed = '%s to %s' % (number(spec.get('min', '')), number(spec.get('max', '')))
        elif kind == 'bool':
            allowed = 'yes or no'
            default = 'yes' if default else 'no'
        elif kind == 'ids':
            allowed = 'list of covariate IDs'
            default = ', '.join(default) if default else 'none'
        else:
            allowed = 'text'
        if isinstance(default, (int, float)) and not isinstance(default, bool):
            default = number(default)
        rows.append('| %s | `%s` | %s | %s |' % (cell(spec.get('label', key)), key, cell(default if default != '' else 'empty'), cell(allowed)))
    return rows


def render(registry):
    lines = ['# Methods reference', '',
             'This guide lists every method in the method registry (`backend/sps/registry.json`, version %s) '
             'with its parameters, defaults and limits. It is generated by `tools/make_methods_doc.py`; '
             'do not edit it by hand.' % registry['registry_version'], '',
             'Status "Available" means the method passed the automated backend acceptance tests of this '
             'version. "Not implemented" methods are listed for completeness and cannot be selected. '
             'Methods marked "experimental" passed the same tests but have known limits that are stated '
             'with the method.', '',
             'Every learned step (preprocessing, variable selection, tuning, interval calibration) is fitted '
             'inside the training part of each fold. The outer test observations and a locked independent '
             'test set never take part in these steps.', '']
    lines.append('## Contents')
    lines.append('')
    for key, title, tab, _ in CATEGORIES:
        lines.append('- [%s](#%s) (panel tab %s)' % (title, title.lower().replace(' ', '-').replace('(', '').replace(')', ''), tab))
    lines.append('')
    for key, title, tab, intro in CATEGORIES:
        methods = registry['categories'][key]
        lines += ['## ' + title, '', 'Panel tab: **%s**.' % tab, '', intro, '']
        lines += ['| Method | ID | Status | Description |', '|---|---|---|---|']
        for method in methods:
            status = STATUS.get(method['status'], method['status'])
            if method['status'] == 'backend_verified' and not method['enabled']:
                status = 'Disabled'
            lines.append('| %s | `%s` | %s | %s |' % (cell(method['label']), method['id'], status, cell(method['description'])))
        lines.append('')
        for method in methods:
            if method['status'] == 'not_implemented':
                continue
            lines += ['### %s (`%s`)' % (method['label'], method['id']), '', method['description'], '']
            if method['id'] in INSTALL_NOTES and key in ('validation', 'model'):
                lines += ['Installation: ' + INSTALL_NOTES[method['id']], '']
            if method.get('cost'):
                lines += ['Cost: ' + method['cost'], '']
            if method.get('parameters'):
                lines += ['| Parameter | ID | Default | Allowed values |', '|---|---|---|---|']
                lines += parameter_rows(method['parameters'])
                lines.append('')
            else:
                lines += ['No parameters.', '']
    return '\n'.join(lines).rstrip() + '\n'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args(argv)
    text = render(json.loads(REGISTRY.read_text(encoding='utf-8')))
    if args.check:
        current = TARGET.read_text(encoding='utf-8') if TARGET.is_file() else ''
        if current != text:
            print('docs/METHODS.md is out of date; run python tools/make_methods_doc.py', file=sys.stderr)
            return 1
        return 0
    TARGET.write_text(text, encoding='utf-8')
    print(TARGET)
    return 0


if __name__ == '__main__':
    sys.exit(main())

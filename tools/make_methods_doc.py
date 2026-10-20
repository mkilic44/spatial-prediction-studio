"""Write docs/METHODS.md from the method registry, so the guide always matches the software.

Usage: python tools/make_methods_doc.py [--check]
--check exits with status 1 when docs/METHODS.md is out of date.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / 'backend/sps/registry.json'
REFERENCES = ROOT / 'docs/references.json'
TARGET = ROOT / 'docs/METHODS.md'

# Sources of each method. Every key must have a checked record in docs/references.json
# (tools/references.py, references workflow); a key without one stops the generator.
METHOD_REFERENCES = {
    ('validation', 'group'): ['roberts2017'],
    ('validation', 'spatial_block'): ['roberts2017', 'valavi2019'],
    ('validation', 'buffered'): ['lerest2014', 'roberts2017'],
    ('validation', 'knndm'): ['linnenbrink2024', 'mila2022', 'cast'],
    ('validation', 'temporal'): ['roberts2017'],
    ('preprocessing', 'standard_pipeline'): ['hastie2009'],
    ('target_transform', 'yeo_johnson'): ['yeo2000'],
    ('vif', 'diagnostic'): ['dormann2013'],
    ('vif', 'iterative'): ['dormann2013'],
    ('feature_selection', 'mutual_info'): ['kraskov2004', 'ross2014'],
    ('feature_selection', 'elastic_net'): ['zou2005'],
    ('feature_selection', 'sfs'): ['meyer2018'],
    ('feature_selection', 'rfe'): ['guyon2002'],
    ('feature_selection', 'boruta'): ['kursa2010'],
    ('feature_selection', 'pca'): ['jolliffe2016'],
    ('model', 'ridge'): ['hoerl1970'],
    ('model', 'elastic_net'): ['zou2005'],
    ('model', 'random_forest'): ['breiman2001'],
    ('model', 'extra_trees'): ['geurts2006'],
    ('model', 'svr'): ['smola2004'],
    ('model', 'hist_gradient_boosting'): ['friedman2001', 'ke2017'],
    ('model', 'xgboost'): ['chen2016'],
    ('model', 'lightgbm'): ['ke2017'],
    ('model', 'catboost'): ['prokhorenkova2018'],
    ('model', 'cubist'): ['quinlan1993'],
    ('model', 'ordinary_kriging'): ['webster2007'],
    ('model', 'regression_kriging'): ['odeh1995', 'hengl2004'],
    ('model', 'tabpfn'): ['hollmann2025'],
    ('model', 'tabm'): ['gorishniy2024'],
    ('model', 'realmlp'): ['holzmuller2024'],
    ('optimization', 'random'): ['bergstra2012'],
    ('optimization', 'optuna'): ['akiba2019'],
    ('uncertainty', 'split_conformal'): ['lei2018'],
    ('uncertainty', 'quantile_forest'): ['meinshausen2006', 'johnson2024'],
    ('uncertainty', 'quantile_boosting'): ['friedman2001', 'koenker1978'],
    ('uncertainty', 'cqr'): ['romano2019'],
    ('uncertainty', 'spatial_conformal'): ['tibshirani2019', 'guan2023'],
}
METHOD_NOTES = {
    ('uncertainty', 'spatial_conformal'):
        'The method is not an implementation of the spatial conformal prediction of {mao2024}. '
        'The coverage results of these papers are not claimed for it.',
}
# Sources of the diagnostics and metrics described in docs/OUTPUTS.md and docs/USER_GUIDE.md.
OTHER_REFERENCES = [
    ('Area of applicability (AOA) and dissimilarity index (DI)', ['meyer2021', 'cast']),
    ('Permutation variable importance', ['breiman2001', 'fisher2019']),
    ("Lin's concordance correlation coefficient (CCC)", ['lin1989']),
    ('RPD and RPIQ', ['bellonmaurel2010']),
    ('Interval score', ['gneiting2007']),
    ('Pinball (quantile) loss', ['koenker1978']),
    ('Design-based map validation with a probability sample (listed, not implemented)', ['brus2011', 'wadoux2021']),
]

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


def load_references():
    return json.loads(REFERENCES.read_text(encoding='utf-8'))['references']


def record(references, key):
    entry = references.get(key)
    if not entry or not entry.get('metadata'):
        raise SystemExit('Reference %r has no checked record in docs/references.json; '
                         'run the references workflow (tools/references.py update) first.' % key)
    return entry


def authors_of(entry):
    """Authors of the checked record, plus documented supplements for records that list too few."""
    return entry['metadata']['authors'] + entry.get('supplement', {}).get('authors', [])


def names(entry):
    families = [author[0] for author in authors_of(entry)]
    return families[0] if len(families) == 1 else ('%s and %s' % tuple(families) if len(families) == 2 else families[0] + ' et al.')


def year_labels(references, keys):
    """Year as cited; works with the same names and year get a, b, ... in reference list order."""
    groups = {}
    for key in sorted(keys, key=lambda key: (record(references, key)['metadata']['title'].lower(), key)):
        meta = record(references, key)['metadata']
        groups.setdefault((names(record(references, key)), meta['year']), []).append(key)
    labels = {}
    for (_, year), members in groups.items():
        for index, key in enumerate(members):
            labels[key] = '%s%s' % (year, 'abcdefghij'[index] if len(members) > 1 else '')
    return labels


YEARS = {}


def short(references, key):
    """In-text citation with a link to the reference list, for example 'Roberts et al. (2017)'."""
    meta = record(references, key)['metadata']
    locator = record(references, key).get('locator')
    return '[%s (%s%s)](#ref-%s)' % (names(record(references, key)), YEARS.get(key, meta['year']), ', ' + locator if locator else '', key)


def cite(references, keys):
    return ', '.join(short(references, key) for key in keys)


def full(references, key):
    entry = record(references, key)
    meta = entry['metadata']
    authors = ['%s, %s' % (family, given) if given else family for family, given in authors_of(entry)]
    author_text = ', '.join(authors[:6]) + (', et al.' if len(authors) > 6 else '')
    title = meta['title'].rstrip('.')
    text = '%s (%s). %s.' % (author_text, YEARS.get(key, meta['year']), title)
    if meta['container'] and meta['type'] not in ('book', 'monograph'):
        text += ' *%s*' % meta['container']
        if meta['volume']:
            text += ', %s' % meta['volume'] + ('(%s)' % meta['issue'] if meta['issue'] else '')
        if meta['pages']:
            text += ', %s' % meta['pages']
        text += '.'
    elif meta['publisher']:
        text += ' %s.' % meta['publisher']
    text += ' ' + meta['url']
    if entry.get('supplement'):
        text += ' (The DOI record names only %s; the other authors are taken from %s.)' % (
            meta['authors'][0][0], entry['supplement']['source'])
    return '- <a name="ref-%s"></a>%s  \n  Cited for: %s' % (key, text, entry['used_for'])


def cited_keys():
    keys = {key for value in METHOD_REFERENCES.values() for key in value}
    keys |= {key for _, value in OTHER_REFERENCES for key in value}
    keys |= {'hastie2009', 'varma2006'} | {key for note in METHOD_NOTES.values() for key in re.findall(r'\{(\w+)\}', note)}
    return keys


def render(registry, references=None):
    references = load_references() if references is None else references
    YEARS.clear()
    YEARS.update(year_labels(references, cited_keys()))
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
             'test set never take part in these steps (see %s).' % cite(references, ['hastie2009', 'varma2006']), '',
             'Each method names its sources; the [references](#references) at the end come from the DOI '
             'registration or the publisher page and were checked automatically.', '']
    lines.append('## Contents')
    lines.append('')
    for key, title, tab, _ in CATEGORIES:
        lines.append('- [%s](#%s) (panel tab %s)' % (title, title.lower().replace(' ', '-').replace('(', '').replace(')', ''), tab))
    lines.append('- [Sources of diagnostics and metrics](#sources-of-diagnostics-and-metrics)')
    lines.append('- [References](#references)')
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
            if (key, method['id']) in METHOD_REFERENCES:
                lines += ['References: ' + cite(references, METHOD_REFERENCES[key, method['id']]) + '.', '']
            if (key, method['id']) in METHOD_NOTES:
                note = METHOD_NOTES[key, method['id']]
                for cited in re.findall(r'\{(\w+)\}', note):
                    note = note.replace('{%s}' % cited, short(references, cited))
                lines += [note, '']
            if method.get('parameters'):
                lines += ['| Parameter | ID | Default | Allowed values |', '|---|---|---|---|']
                lines += parameter_rows(method['parameters'])
                lines.append('')
            else:
                lines += ['No parameters.', '']
    lines += ['## Sources of diagnostics and metrics', '',
              'The diagnostics and metrics are described in [OUTPUTS.md](OUTPUTS.md) and [USER_GUIDE.md](USER_GUIDE.md).', '',
              '| Diagnostic or metric | Sources |', '|---|---|']
    lines += ['| %s | %s |' % (cell(name), cite(references, keys)) for name, keys in OTHER_REFERENCES]
    lines += ['', '## References', '',
              'Each record below comes from the DOI registration (doi.org) or, for a paper without DOI, from the '
              'citation tags of the publisher page. `tools/references.py` checks in the references workflow that '
              'each one is the expected work. "Cited for" states what the source supports here.', '']
    cited = cited_keys()
    def order(key):
        meta = record(references, key)['metadata']
        return (meta['authors'][0][0].lower() if meta['authors'] else '', str(YEARS.get(key, meta['year'])), key)
    lines += [full(references, key) for key in sorted(cited, key=order)]
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

"""Fetch and check the bibliographic records of docs/references.json.

Every reference names a DOI (or, for a paper without DOI, its publisher page) and what it is expected
to be: first author's family name, year and words of the title. The records written to the
documentation come from the DOI registration (doi.org, CSL JSON) or from the citation_* tags of the
publisher page, never from memory. A record whose metadata does not match the expectation fails.

Usage:
    python tools/references.py update   fetch every record, check it and write docs/references.json
    python tools/references.py check    fetch every record and compare it with docs/references.json

Both commands need internet access to doi.org and the publisher pages; they run in CI.
"""
from __future__ import annotations

import argparse
import html
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import sys
import time
import unicodedata
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / 'docs/references.json'
DASHES = dict.fromkeys(map(ord, '\u2010\u2011\u2012\u2013\u2014\u2015\u2212'), '-')
USER_AGENT = 'SpatialPredictionStudio-reference-check/1.0 (https://github.com/mkilic44/spatial-prediction-studio)'


def clean(text):
    """Plain text without markup or entities, with ASCII hyphens instead of dashes and single spaces."""
    text = html.unescape(re.sub(r'<[^>]+>', '', str(text or '')))
    text = re.sub(r'\s+', ' ', text.translate(DASHES)).strip()
    # Markup in registry titles leaves spaces such as "k -fold" or "CV :".
    text = re.sub(r'(\w) -(\w)', r'\1-\2', text)
    return re.sub(r'\s+([:;,.?!])', r'\1', text)


def simple(text):
    text = unicodedata.normalize('NFKD', clean(text)).encode('ascii', 'ignore').decode('ascii')
    return re.sub(r'[^a-z0-9 ]', ' ', text.lower()).split()


def fetch(url, accept):
    request = urllib.request.Request(url, headers={'Accept': accept, 'User-Agent': USER_AGENT})
    last = None
    for attempt in range(4):
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return response.read().decode('utf-8', errors='replace')
        except OSError as error:
            last = error
            time.sleep(2 ** attempt)
    raise RuntimeError('could not fetch %s: %s' % (url, last))


def initials(given):
    """'Hanna' -> 'H.', 'In-Kwon' -> 'I.-K.', 'J. J.' -> 'J. J.'"""
    words = [word for word in re.split(r'[\s.]+', clean(given)) if word]
    return ' '.join('-'.join(part[0] + '.' for part in word.split('-') if part) for word in words)


def from_csl(data, doi):
    authors = []
    for person in data.get('author', []) or data.get('editor', []):
        if person.get('family'):
            authors.append([clean(person['family']), initials(person.get('given', ''))])
        elif person.get('literal') or person.get('name'):
            authors.append([clean(person.get('literal') or person.get('name')), ''])
    # The year of the printed volume when the registry has it, otherwise the date of issue.
    parts = (data.get('published-print') or data.get('issued') or data.get('published-online') or {}).get('date-parts') or [[None]]
    container = data.get('container-title') or ''
    if isinstance(container, list):
        container = container[0] if container else ''
    title = clean(data.get('title', ''))
    subtitle = data.get('subtitle') or []
    subtitle = clean(subtitle[0] if isinstance(subtitle, list) and subtitle else subtitle)
    if subtitle and subtitle.lower() not in title.lower():
        title = title.rstrip(':') + ': ' + subtitle
    return {'authors': authors, 'year': parts[0][0], 'title': title,
            'container': clean(container), 'volume': clean(data.get('volume', '')), 'issue': clean(data.get('issue', '')),
            'pages': clean(data.get('page', '')), 'publisher': clean(data.get('publisher', '')),
            'type': data.get('type', ''), 'doi': doi.lower(), 'url': 'https://doi.org/' + doi}


class CitationTags(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = {}

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        name = (attrs.get('name') or '').lower()
        if tag == 'meta' and name.startswith('citation_') and attrs.get('content'):
            self.tags.setdefault(name, []).append(attrs['content'])


def from_page(html, url):
    parser = CitationTags()
    parser.feed(html)
    tags = parser.tags
    authors = []
    for name in tags.get('citation_author', []):
        if ',' in name:
            family, given = name.split(',', 1)
        else:
            given, _, family = name.rpartition(' ')
        authors.append([clean(family), initials(given)])
    date = (tags.get('citation_publication_date') or tags.get('citation_date') or [''])[0]
    year = int(re.search(r'\d{4}', date).group()) if re.search(r'\d{4}', date) else None
    container = (tags.get('citation_journal_title') or tags.get('citation_conference_title') or [''])[0]
    first, last = (tags.get('citation_firstpage') or [''])[0], (tags.get('citation_lastpage') or [''])[0]
    return {'authors': authors, 'year': year, 'title': clean((tags.get('citation_title') or [''])[0]),
            'container': clean(container), 'volume': clean((tags.get('citation_volume') or [''])[0]),
            'issue': clean((tags.get('citation_issue') or [''])[0]),
            'pages': clean(first + ('-' + last if last else '')), 'publisher': '',
            'type': 'paper-conference' if tags.get('citation_conference_title') else 'article-journal',
            'doi': '', 'url': url}


def openalex_authors(doi):
    """Author list of a DOI in OpenAlex, as [family, initials]."""
    data = json.loads(fetch('https://api.openalex.org/works/https://doi.org/' + doi, 'application/json'))
    result = []
    for item in data.get('authorships', []):
        name = clean((item.get('author') or {}).get('display_name', ''))
        given, _, family = name.rpartition(' ')
        result.append([family, initials(given)])
    return result


def record(entry):
    if entry.get('doi'):
        data = json.loads(fetch('https://doi.org/' + entry['doi'], 'application/vnd.citationstyles.csl+json'))
        metadata = from_csl(data, entry['doi'])
        metadata['authors_source'] = 'doi'
        if len(metadata['authors']) == 1 and data.get('type') not in ('dataset',):
            # Some DOI records list only the first author. OpenAlex completes the list when its first
            # author is the same person; the record then says where the authors come from.
            try:
                others = openalex_authors(entry['doi'])
            except RuntimeError:
                others = []
            if len(others) > 1 and simple(others[0][0]) == simple(metadata['authors'][0][0]):
                metadata['authors'] = metadata['authors'] + others[1:]
                metadata['authors_source'] = 'doi+openalex'
        return metadata
    metadata = from_page(fetch(entry['url'], 'text/html'), entry['url'])
    metadata['authors_source'] = 'publisher page'
    return metadata


def problems(key, entry, metadata):
    expect, found = entry['expect'], []
    family = metadata['authors'][0][0] if metadata['authors'] else ''
    if simple(family) != simple(expect['family']):
        found.append('first author %r, expected %r' % (family, expect['family']))
    if not metadata['year'] or abs(int(metadata['year']) - int(expect['year'])) > 1:
        found.append('year %r, expected %r (one year apart is accepted: online and print dates)' % (metadata['year'], expect['year']))
    title = ' '.join(simple(metadata['title']))
    if ' '.join(simple(expect['title'])) not in title:
        found.append('title %r does not contain %r' % (metadata['title'], expect['title']))
    return ['%s: %s' % (key, item) for item in found]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('command', choices=('update', 'check'))
    args = parser.parse_args(argv)
    document = json.loads(TARGET.read_text(encoding='utf-8'))
    failures, changed = [], []
    for key, entry in document['references'].items():
        try:
            metadata = record(entry)
        except (RuntimeError, ValueError, KeyError) as error:
            failures.append('%s: %s' % (key, error))
            continue
        failures += problems(key, entry, metadata)
        if entry.get('metadata') != metadata:
            changed.append(key)
            if args.command == 'update':
                entry['metadata'] = metadata
        print('%-18s %s %s. %s' % (key, metadata['year'], (metadata['authors'] or [['?']])[0][0], metadata['title'][:80]))
    if args.command == 'update':
        TARGET.write_text(json.dumps(document, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    for item in failures:
        print('FAILED ' + item)
    if args.command == 'check' and changed:
        print('Stored metadata differs from the registry for: ' + ', '.join(changed))
    return 1 if failures or (args.command == 'check' and changed) else 0


if __name__ == '__main__':
    sys.exit(main())

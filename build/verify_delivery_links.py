"""Verify every local HTML link/image in the portable delivery, without opening a browser."""
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
from urllib.parse import unquote, urlsplit

OUT = Path(__file__).resolve().parents[1] / 'release'


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.references = []
        self.ids = set()

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if values.get('id'):
            self.ids.add(values['id'])
        for key in ('href', 'src'):
            if values.get(key):
                self.references.append(values[key])


def main():
    pages = {}
    for path in sorted(OUT.rglob('*.html')):
        parser = Links()
        parser.feed(path.read_text(encoding='utf-8-sig'))
        pages[path.resolve()] = parser
    checked, external, fragments = 0, 0, 0
    for source, parser in pages.items():
        for href in parser.references:
            parts = urlsplit(href)
            if parts.scheme in ('https', 'http', 'mailto', 'data'):
                external += 1
                continue
            assert not parts.scheme and not parts.netloc, (source, href)
            target = (source.parent / unquote(parts.path)).resolve() if parts.path else source
            assert target.is_relative_to(OUT.resolve()), (source, href)
            assert target.is_file(), (source, href)
            checked += 1
            if parts.fragment and target in pages:
                assert unquote(parts.fragment) in pages[target].ids, (source, href)
                fragments += 1
    record = {'status': 'passed', 'html_files': len(pages), 'local_references_checked': checked,
              'html_fragments_checked': fragments, 'external_or_embedded_references_not_fetched': external,
              'browser_visual_check': 'not_performed',
              'sources': {str(p.relative_to(OUT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in pages}}
    (OUT / 'report-links-readback.json').write_text(json.dumps(record, indent=2), encoding='utf-8')
    print(json.dumps({k: v for k, v in record.items() if k != 'sources'}))


if __name__ == '__main__':
    main()

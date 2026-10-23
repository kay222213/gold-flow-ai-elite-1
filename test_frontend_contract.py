from html.parser import HTMLParser
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]

class PageParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = []
        self.buttons = []
    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if attrs.get('id'):
            self.ids.append(attrs['id'])
        if tag == 'button':
            self.buttons.append(attrs)

def test_frontend_all_static_id_references_exist_and_are_unique():
    html = (ROOT / 'frontend' / 'index.html').read_text(encoding='utf-8')
    parser = PageParser(); parser.feed(html)
    assert len(parser.ids) == len(set(parser.ids)), 'duplicate DOM IDs break controls'
    refs = set(re.findall(r"\$\(['\"]([A-Za-z0-9_-]+)['\"]\)", html))
    assert refs.issubset(set(parser.ids)), f'missing DOM IDs: {sorted(refs-set(parser.ids))}'

def test_every_named_action_button_has_functionality_wired():
    html = (ROOT / 'frontend' / 'index.html').read_text(encoding='utf-8')
    js = (ROOT / 'frontend' / 'app-features.js').read_text(encoding='utf-8')
    for control in ['copyPlanBtn', 'clearSignalHistory', 'runBacktestBtn', 'downloadBacktestBtn']:
        assert control in js and 'addEventListener' in js, f'{control} missing handler in app-features.js'
    assert 'runBaselineBacktest' in js
    assert 'gfa_signal_history_v1' in js
    assert './app-features.js?v=11' in html

def test_frontend_does_not_bundle_provider_secrets_or_default_to_phone_localhost():
    html = (ROOT / 'frontend' / 'index.html').read_text(encoding='utf-8')
    assert 'TWELVEDATA_API_KEY' not in html
    assert '127.0.0.1:8000' not in html
    assert "location.hostname.endsWith('github.io')" in html

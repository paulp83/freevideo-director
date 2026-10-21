"""Offline check of docs/ui-capability-map.md: every Selector token exists in project.html.

The browser suites walk these selectors in a real render; this test fails fast (no browser)
when the documentation names a control that is not actually in the page, and when a row is
still marked as pending work.
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOC = ROOT / 'docs' / 'ui-capability-map.md'
PAGE = ROOT / 'project.html'


def table_rows(text):
    rows = []
    for line in text.splitlines():
        if not line.startswith('|'):
            continue
        cells = [c.strip() for c in line.strip().strip('|').split('|')]
        if len(cells) != 6 or 'Old control' in cells[0] or set(cells[0]) <= set('-: '):
            continue
        rows.append(cells)
    return rows


def selector_tokens(cell):
    return [t.strip() for g in re.findall(r'`([^`]+)`', cell) for t in g.split(',') if t.strip()]


def token_exists(tok, page):
    """Resolve one Selector token against the page source."""
    if tok.startswith('#'):
        ident = tok[1:]
        return (f'id="{ident}"' in page or f"id='{ident}'" in page
                or f'id: "{ident}"' in page or f"id: '{ident}'" in page)
    m = re.fullmatch(r'\[data-k\s*=\s*"?([^"\]]+)"?\]', tok)
    if m:
        return 'data-k' in page and m.group(1) in page
    if tok[:1] in '.[':
        return tok in page or tok.lstrip('.').split(' ')[-1] in page
    return tok in page


class CapabilityMap(unittest.TestCase):
    def setUp(self):
        self.doc = DOC.read_text()
        self.page = PAGE.read_text()
        self.rows = table_rows(self.doc)

    def test_table_parsed(self):
        self.assertGreater(len(self.rows), 50, 'capability map table did not parse')

    def test_nothing_left_pending(self):
        pending = [r[0] for r in self.rows
                   if 'pending' in r[3].lower() or 'not yet' in r[3].lower()]
        self.assertEqual(pending, [], 'rows still marked pending: ' + '; '.join(pending))

    def test_rows_have_selectors_and_verifiers(self):
        for cells in self.rows:
            what, status, selector, verified = cells[0], cells[3], cells[4], cells[5]
            self.assertTrue(re.match(r'(kept|moved|live)', status.lower()),
                            f'{what}: unexpected status {status!r}')
            self.assertTrue(selector_tokens(selector), f'{what}: empty Selector column')
            self.assertTrue(verified, f'{what}: empty Verified by column')

    def test_every_selector_token_exists_in_page(self):
        bad = []
        for cells in self.rows:
            for tok in selector_tokens(cells[4]):
                if not token_exists(tok, self.page):
                    bad.append(f'{cells[0][:40]}: {tok}')
        self.assertEqual(bad, [], 'selector tokens missing from project.html:\n' + '\n'.join(bad))


if __name__ == '__main__':
    unittest.main()

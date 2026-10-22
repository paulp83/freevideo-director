#!/usr/bin/env python3
"""The director retry loop: one retry quoting check names and messages, better reply wins,
empty final replies raise with the check names."""
import unittest
from pathlib import Path
from unittest.mock import patch

import director

GOOD = (Path(__file__).resolve().parent / 'fixtures' / 'h3' / 't2va-boat.txt').read_text().strip()
IDEA = 'a paper boat drifts down a rain gutter into a puddle'


class Stub:
    """Replays canned chat replies and records the `extra` messages of each call."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def chat(self, url, model, system, content, think, temperature, max_tokens=12000, extra=(), **kw):
        self.calls.append([m['content'] for m in extra])
        return self.replies[min(len(self.calls) - 1, len(self.replies) - 1)], 7


def run(stub, **kw):
    with patch.object(director, 'backend', lambda: ('http://x/v1/chat/completions', 'm')), \
         patch.object(director, 'chat', stub.chat):
        return director.direct_report(IDEA, seconds=6, think=False, **kw)


class Retry(unittest.TestCase):
    def test_a_passing_reply_gets_no_second_call(self):
        stub = Stub([GOOD])
        rep = run(stub)
        self.assertEqual(len(stub.calls), 1)
        self.assertFalse(rep['retried'])
        self.assertEqual(rep['final_failures'], [])
        self.assertEqual(rep['text'], GOOD)

    def test_a_failing_reply_comes_back_naming_checks_with_their_messages(self):
        stub = Stub(["I cannot write that.", GOOD])
        rep = run(stub)
        self.assertTrue(rep['retried'])
        self.assertEqual(rep['text'], GOOD)
        self.assertEqual(rep['final_failures'], [])
        self.assertIn('refusal_only', rep['first_failures'])
        self.assertIn('fields_present', rep['first_failures'])
        fix = stub.calls[1][1]                                   # the retry user turn
        self.assertIn('fields_present: missing section', fix)
        self.assertIn('the reply is only a refusal or apology', fix)
        self.assertIn('output ONLY the corrected final prompt text', fix)
        self.assertEqual(stub.calls[1][0], "I cannot write that.")   # the first reply is resent

    def test_an_empty_first_reply_is_retried_too(self):
        stub = Stub(['', GOOD])
        rep = run(stub)
        self.assertIn('empty_reply', rep['first_failures'])
        self.assertEqual(rep['text'], GOOD)
        self.assertTrue(rep['retried'])

    def test_on_a_tie_the_later_reply_wins(self):
        first = '# a\n\n' + GOOD                                 # fails no_markdown + opening line
        second = '## b\n\n' + GOOD                               # fails exactly the same checks
        stub = Stub([first, second])
        rep = run(stub)
        self.assertEqual(rep['text'], second)
        self.assertEqual(set(rep['final_failures']), {'no_markdown', 'no_instruction_line'})

    def test_a_still_failing_reply_is_returned_with_its_check_names(self):
        stub = Stub(["I cannot write that.", "I'm sorry but I can't."])
        rep = run(stub)
        self.assertTrue(rep['retried'])
        self.assertIn('refusal_only', rep['first_failures'])
        self.assertIn('refusal_only', rep['final_failures'])

    def test_an_empty_final_reply_raises_naming_the_checks(self):
        stub = Stub(['', ''])
        with self.assertRaises(director.DirectorError) as cm:
            run(stub)
        self.assertIn('empty_reply', str(cm.exception))

    def test_retries_off_means_one_call_even_when_the_reply_fails(self):
        stub = Stub(["I cannot write that."])
        rep = run(stub, retries=0)
        self.assertEqual(len(stub.calls), 1)
        self.assertFalse(rep['retried'])
        self.assertTrue(rep['final_failures'])

    def test_tokens_accumulate_across_the_retry(self):
        stub = Stub(["I cannot write that.", GOOD])
        rep = run(stub)
        self.assertEqual(rep['tokens'], 14)


if __name__ == '__main__':
    unittest.main()

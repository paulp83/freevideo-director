#!/usr/bin/env python3
"""h3check rules: real saved director outputs pass; deliberate mutations fail the named check."""
import unittest
from pathlib import Path

import h3check

FIX = Path(__file__).resolve().parent / 'fixtures' / 'h3'
PASSING = [('t2va-boat.txt', 't2va', 6, 0),
           ('t2va-dialogue.txt', 't2va', 10, 0),
           ('i2va-cheer.txt', 'i2va', 5, 1),
           ('ref2va-lightsaber.txt', 'ref2va', 10, 1)]


def load(name):
    return (FIX / name).read_text()


def fails(text, mode='t2va', seconds=6, n_images=0):
    return set(h3check.failures(mode, text, seconds, n_images))


class RealOutputs(unittest.TestCase):
    def test_saved_passing_outputs_still_pass_every_applicable_check(self):
        for name, mode, seconds, images in PASSING:
            self.assertEqual(h3check.failures(mode, load(name), seconds, images), [], name)

    def test_the_real_missing_label_output_fails_the_named_checks(self):
        bad = set(h3check.failures('t2va', load('t2va-missing-label.txt'), 10, 0))
        self.assertEqual(bad, {'fields_present', 'no_instruction_line', 'shot_numbering'})

    def test_report_shape_matches_applicable(self):
        for name, mode, seconds, images in PASSING:
            rep = h3check.report(mode, load(name), seconds, images)
            self.assertEqual([r['check'] for r in rep], h3check.applicable(mode), name)
            self.assertTrue(all(r['ok'] and r['detail'] == '' for r in rep), name)

    def test_junk_replies_never_pass_and_every_failure_has_a_one_line_detail(self):
        for mode in ('t2va', 'i2va', 'l2va', 'fl2va', 'ref2va'):
            for text in ('hello world', '', None, '```'):
                rep = [r for r in h3check.report(mode, text, 10, 1) if not r['ok']]
                self.assertTrue(rep, (mode, text))
                for r in rep:
                    self.assertTrue(r['detail'], (mode, text, r['check']))
                    self.assertNotIn('\n', r['detail'])


class ReplyKinds(unittest.TestCase):
    def setUp(self):
        self.boat = load('t2va-boat.txt')

    def test_an_empty_reply_is_flagged_as_empty(self):
        for text in ('', '   ', None):
            self.assertIn('empty_reply', fails(text))

    def test_a_bare_code_fence_is_flagged_as_fence_only(self):
        self.assertIn('code_fence_only', fails('```'))

    def test_a_prompt_wrapped_in_a_whole_reply_fence_is_normalised(self):
        self.assertEqual(fails('```\n' + self.boat + '\n```'), set())

    def test_a_refusal_instead_of_a_prompt_is_named(self):
        self.assertIn('refusal_only', fails("I'm sorry, but I can't write a video prompt about that."))

    def test_an_apology_beside_a_real_prompt_is_not_a_refusal(self):
        self.assertNotIn('refusal_only', fails('Sorry for the delay!\n\n' + self.boat))

    def test_a_heading_is_markdown(self):
        self.assertEqual(fails('# Title\n\n' + self.boat), {'no_markdown', 'no_instruction_line'})


class Sections(unittest.TestCase):
    def setUp(self):
        self.boat = load('t2va-boat.txt')

    def test_a_missing_section_is_named(self):
        start = self.boat.index('overall_soundscape:')
        end = self.boat.index('non_diegetic_music:')
        self.assertEqual(fails(self.boat[:start] + self.boat[end:]), {'fields_present'})

    def test_sections_out_of_order_are_named(self):
        head, sep, tail = self.boat.partition('overall_soundscape:')
        mid, sep2, end = tail.partition('non_diegetic_music:')
        text = head + 'non_diegetic_music:' + end + '\n\noverall_soundscape:' + mid
        self.assertEqual(fails(text), {'fields_in_order'})

    def test_a_duplicated_section_is_named_once(self):
        tail = self.boat[self.boat.index('non_diegetic_music:'):]
        self.assertEqual(fails(self.boat + '\n\n' + tail), {'fields_once'})

    def test_a_section_with_no_content_after_the_label(self):
        cut = self.boat.rindex('non_diegetic_music:')
        self.assertEqual(fails(self.boat[:cut] + 'non_diegetic_music:\n'), {'fields_non_empty'})

    def test_na_is_valid_audio_content(self):
        head = self.boat[:self.boat.index('non_diegetic_music:')]
        self.assertEqual(fails(head + 'non_diegetic_music: N/A'), set())

    def test_an_unknown_section_is_named_without_breaking_order(self):
        self.assertEqual(fails(self.boat + '\n\nnotes: something'), {'unknown_section'})

    def test_ref2va_requires_all_six_sections_in_order(self):
        text = load('ref2va-lightsaber.txt').replace('retention_analysis:', 'leftovers:')
        self.assertEqual(fails(text, 'ref2va', 10, 1), {'fields_present', 'unknown_section'})


class ShotsAndStamps(unittest.TestCase):
    def setUp(self):
        self.boat = load('t2va-boat.txt')

    def test_a_malformed_stamp_is_named(self):
        self.assertEqual(fails(self.boat.replace('At 00:04.000', 'At 0:5')), {'timestamps_format'})

    def test_a_shot_label_without_its_cut_time(self):
        self.assertEqual(fails(self.boat.replace('[Shot 2] At 00:04.000,', '[Shot 2]')), {'shot_cuts'})

    def test_a_cut_time_inside_shot_one_is_named(self):
        text = self.boat.replace('drifts past a rusted nail.',
                                 'drifts past a rusted nail. At 00:02.000, the boat tilts.')
        self.assertEqual(fails(text), {'first_shot_zero'})

    def test_a_stray_cut_time_with_no_shot_label(self):
        text = self.boat.replace('The paper boat rides', 'At 00:05.000, the paper boat rides')
        self.assertEqual(fails(text), {'shot_cuts'})

    def test_shot_numbers_must_not_skip_a_number(self):
        self.assertEqual(fails(self.boat.replace('[Shot 2]', '[Shot 3]')), {'shot_numbering'})

    def test_duplicated_shot_numbers_are_named(self):
        self.assertIn('shot_numbering', fails(self.boat.replace('[Shot 2]', '[Shot 1]')))

    def test_a_cut_at_or_past_the_duration_is_named(self):
        self.assertEqual(fails(self.boat.replace('At 00:04.000', 'At 00:06.000')),
                         {'timestamps_in_range'})

    def test_cut_times_must_strictly_increase(self):
        text = load('t2va-dialogue.txt').replace('At 00:04.000', 'At 00:08.000') \
            .replace('At 00:07.500', 'At 00:04.000')
        self.assertEqual(fails(text, 't2va', 10, 0), {'timestamps_monotonic'})


class InstructionAndLabels(unittest.TestCase):
    def test_i2va_must_open_with_the_exact_instruction(self):
        cheer = load('i2va-cheer.txt')
        text = cheer.replace(
            'For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) '
            'is fully referenced.',
            'For the target video, at 0.00 seconds, <Picture 1> is referenced.')
        self.assertEqual(fails(text, 'i2va', 5, 1), {'instruction_line'})

    def test_i2va_shot_one_must_reference_the_picture(self):
        cheer = load('i2va-cheer.txt')
        self.assertEqual(fails(cheer.replace('the four individuals from <Picture 1> stand',
                                             'the four people stand'), 'i2va', 5, 1),
                         {'picture1_in_shot1'})
    def test_t2va_must_not_reference_pictures(self):
        self.assertEqual(fails(load('t2va-boat.txt').replace('[Shot 2]', '<Picture 1> [Shot 2]')),
                         {'labels_valid'})

    def test_labels_must_match_the_number_of_images(self):
        self.assertIn('labels_valid', fails(load('i2va-cheer.txt'), 'i2va', 5, 2))   # <Picture 2> unused

    def test_l2va_instruction_binds_the_final_shot_and_duration(self):
        good = ('How the reference pictures align with the target video — <Picture 1> (from [Shot 2]) '
                'aligns with the 6.00-second mark of the target video.\n\n' + load('t2va-boat.txt'))
        self.assertEqual(fails(good, 'l2va', 6, 1), set())
        self.assertEqual(fails(good.replace('the 6.00-second', 'the 5.00-second'), 'l2va', 6, 1),
                         {'instruction_line'})
        self.assertEqual(fails(good.replace('(from [Shot 2])', '(from [Shot 1])'), 'l2va', 6, 1),
                         {'instruction_line'})

    def test_fl2va_instruction_is_written_without_angle_brackets(self):
        good = ('How the reference pictures align with the target video — Picture 1 (from Shot 1) '
                'aligns with the 0.00-second mark of the target video; Picture 2 (from Shot 2) '
                'aligns with the 6.00-second mark of the target video.\n\n' + load('t2va-boat.txt'))
        self.assertEqual(fails(good, 'fl2va', 6, 2), set())
        self.assertEqual(fails(good.replace('Picture 1 (from Shot 1)', '<Picture 1> (from Shot 1)'),
                               'fl2va', 6, 2), {'instruction_line'})


class Ref2va(unittest.TestCase):
    def setUp(self):
        self.saber = load('ref2va-lightsaber.txt')

    def test_picture_labels_must_be_defined_in_subject_definitions(self):
        text = self.saber.replace('side of <Picture 1>, wearing', 'side of the photo, wearing')
        self.assertEqual(fails(text, 'ref2va', 10, 1), {'picture_labels_defined'})

    def test_summary_must_open_with_a_known_task_type(self):
        self.assertEqual(fails(self.saber.replace('[reference generation]', '[a cool edit]'),
                               'ref2va', 10, 1), {'summary_task_prefix'})
        self.assertEqual(fails(self.saber.replace('[reference generation]',
                                                  '[reference generation + reference generation]'),
                               'ref2va', 10, 1), {'summary_task_prefix'})

    def test_combined_task_types_are_valid(self):
        text = self.saber.replace('[reference generation]', '[video continuation + reference generation]')
        self.assertEqual(fails(text, 'ref2va', 10, 1), set())

    def test_skill_tags_are_not_placeholders(self):
        text = self.saber.replace('clash blades.', 'clash blades <scenetrans>, the hum continues.')
        self.assertEqual(fails(text, 'ref2va', 10, 1), set())


class DialogueAndPlaceholders(unittest.TestCase):
    def setUp(self):
        self.dialogue = load('t2va-dialogue.txt')
    def test_an_unclosed_dialogue_tag_is_named(self):
        self.assertEqual(fails(self.dialogue.replace('</d>', '', 1), 't2va', 10, 0), {'dialogue_tags'})

    def test_an_empty_dialogue_block_is_named(self):
        bad = fails(self.dialogue + ' The man says <d></d> as he leaves.', 't2va', 10, 0)
        self.assertEqual(bad, {'dialogue_tags'})

    def test_leftover_template_placeholders_are_named(self):
        for marker in ('<SUBJECT 1>', '<...>', 'TODO'):
            self.assertIn('no_placeholders', fails(self.dialogue + ' ' + marker, 't2va', 10, 0), marker)


if __name__ == '__main__':
    unittest.main()

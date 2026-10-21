#!/usr/bin/env python3
"""finishing.py: the validation, the one frame-rate policy, and the legacy mapping."""
import unittest

import finishing


def settings(upscale=None, interp=None, encode=None, policy=None):
    raw = {}
    if upscale is not None:
        raw['upscale'] = upscale
    if interp is not None:
        raw['interpolation'] = interp
    if encode is not None:
        raw['encode'] = encode
    if policy is not None:
        raw['fps_policy'] = policy
    return finishing.clean_finishing(raw or None)


class Finishing(unittest.TestCase):
    def test_defaults_are_the_pipeline_as_it_ran_before(self):
        self.assertEqual(finishing.defaults(),
                         {'upscale': {'enabled': True, 'height': 1080},
                          'interpolation': {'enabled': True, 'factor': 2},
                          'encode': {'crf': 16, 'preset': 'high'},
                          'fps_policy': 'interpolated'})
        self.assertTrue(finishing.is_legacy(finishing.for_project({})))     # absent key = old behaviour
        self.assertFalse(finishing.is_legacy(settings(encode={'crf': 20})))

    def test_one_policy_decides_the_output_frame_rate(self):
        for factor, policy, want in ((2, 'interpolated', 48), (4, 'interpolated', 96),
                                     (2, 'original', 24), (4, 'original', 24)):
            s = settings(interp={'factor': factor}, policy=policy)
            self.assertEqual(finishing.output_fps(24, s), want, f'{factor}x {policy}')
        # the policy is derived from the switch when it is not spelled out
        self.assertEqual(finishing.output_fps(25, settings(interp={'enabled': False})), 25)
        self.assertEqual(finishing.output_fps(30, settings(interp={'enabled': True, 'factor': 4})), 120)

    def test_policy_and_interpolation_switch_never_disagree(self):
        self.assertFalse(settings(interp={'enabled': True}, policy='original')['interpolation']['enabled'])
        self.assertTrue(settings(interp={'enabled': False}, policy='interpolated')['interpolation']['enabled'])
        self.assertEqual(settings(interp={'enabled': False})['fps_policy'], 'original')

    def test_validation_names_the_bad_setting(self):
        for raw, word in (({'colour': 1}, 'colour'),
                          ({'upscale': {'width': 720}}, 'width'),
                          ({'upscale': {'height': 900}}, '720, 1080, 2160'),
                          ({'interpolation': {'factor': 3}}, '2, 4'),
                          ({'encode': {'crf': 40}}, 'between 12 and 28'),
                          ({'encode': {'crf': 'wide'}}, 'crf must be a number'),
                          ({'fps_policy': 'cinema'}, 'fps_policy'),
                          (['nope'], 'object')):
            with self.assertRaises(ValueError) as cm:
                finishing.clean_finishing(raw)
            self.assertIn(word, str(cm.exception), str(raw))

    def test_quality_preset_and_crf_are_two_names_for_one_switch(self):
        self.assertEqual(settings(encode={'preset': 'small'})['encode'], {'crf': 24, 'preset': 'small'})
        self.assertEqual(settings(encode={'crf': 20})['encode']['preset'], 'standard')
        self.assertIsNone(settings(encode={'crf': 23})['encode']['preset'])   # custom stays nameless
        self.assertEqual(finishing.settings_key(settings(encode={'preset': 'high'})),
                         finishing.settings_key(settings(encode={'crf': 16})))

    def test_canonical_shape_is_unique_whichever_way_it_is_written(self):
        a = finishing.for_project({'finishing': {'upscale': {'height': 720}, 'encode': {'crf': 20},
                                                 'fps_policy': 'original'}})
        b = settings(upscale={'enabled': True, 'height': 720},
                     interp={'enabled': False, 'factor': 2},
                     encode={'preset': 'standard'}, policy='original')
        self.assertEqual(a, b)

    def test_enabled_stages_and_labels_follow_the_switches(self):
        self.assertEqual(finishing.enabled_stages(finishing.defaults()), ['upscale', 'smooth', 'encode'])
        off = settings(upscale={'enabled': False}, interp={'enabled': False})
        self.assertEqual(finishing.enabled_stages(off), ['encode'])            # encode always runs
        self.assertEqual(finishing.stage_label('upscale', finishing.defaults()), 'Upscale to 1080p')
        self.assertEqual(finishing.stage_label('smooth', finishing.defaults()), 'Smooth to 48 fps')
        self.assertEqual(finishing.stage_label('smooth', settings(interp={'factor': 4})), 'Smooth to 96 fps')

    def test_legacy_snapshots_map_onto_settings_as_recorded_fact(self):
        # an old snapshot only recorded the flat keys -- and they say every stage ran
        old = finishing.settings_of_snapshot({'resolution': 1080, 'multiplier': 2, 'crf': 16, 'fps': 24})
        self.assertEqual(old, finishing.defaults() | {'encode': {'crf': 16, 'preset': None}})
        # a multiplier of 1 is not a guess: it says the take was encoded as rendered
        flat = finishing.settings_of_snapshot({'resolution': 720, 'multiplier': 1, 'crf': 20})
        self.assertFalse(flat['interpolation']['enabled'])
        self.assertEqual(flat['fps_policy'], 'original')
        self.assertEqual(flat['upscale'], {'enabled': True, 'height': 720})
        self.assertIsNone(finishing.settings_of_snapshot(None))
        self.assertIsNone(finishing.settings_of_snapshot({'model': 'seedvr2'}))   # no usable keys
        new = finishing.settings_of_snapshot({'settings': {'upscale': {'enabled': False},
                                                           'fps_policy': 'original'}})
        self.assertFalse(new['upscale']['enabled'])

    def test_a_saved_custom_crf_round_trips_through_validation(self):
        once = finishing.clean_finishing({'encode': {'crf': 18}})
        self.assertEqual(once['encode'], {'crf': 18, 'preset': None})
        # exactly what the editor stores and the browser sends back on the next save
        self.assertEqual(finishing.clean_finishing(once), once)
        self.assertEqual(finishing.clean_finishing({'encode': {'crf': 18, 'preset': None}}), once)

    def test_plan_extras_appear_only_when_the_project_leaves_legacy(self):
        self.assertEqual(finishing.plan_extras(finishing.for_project({})), {})   # old fingerprints hold
        extras = finishing.plan_extras(settings(upscale={'enabled': False}))
        self.assertEqual(extras, {'upscale': {'enabled': False, 'height': 1080},
                                  'interpolation': {'enabled': True, 'factor': 2}})


if __name__ == '__main__':
    unittest.main()

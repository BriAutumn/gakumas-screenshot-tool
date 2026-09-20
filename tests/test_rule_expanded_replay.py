"""Catalog-derived states must still match independently read original pixels."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_rule_short_replay as replay


class ExpandedReplayTests(unittest.TestCase):
    def test_original_pixels(self):
        previous = replay.FIXTURES
        replay.FIXTURES = Path(__file__).resolve().parent / 'fixtures/rule-expanded-effects'
        try:
            replay.RuleShortReplayTests(
                'test_actual_tooltip_sources_without_network_or_vision_import'
            ).test_actual_tooltip_sources_without_network_or_vision_import()
        finally:
            replay.FIXTURES = previous


if __name__ == '__main__':
    unittest.main()

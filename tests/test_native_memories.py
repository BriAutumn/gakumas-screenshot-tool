"""Tests for gkshot.native_memories conversion."""

import unittest

from gkshot.native_memories import (
    ConvertError,
    convert_memory,
    convert_snapshot,
)


class FakeCatalog:
    """Minimal catalog stub used by validate_memory."""

    def __init__(self, available_effects=None):
        self.available_effects = dict(available_effects or {})
        self.skill_cards = {1000: {'pIdolId': 100}, 1001: {'pIdolId': 100},
                           2000: {'pIdolId': ''}, 2001: {'pIdolId': ''}}

    def validate_entities(self, *args, **kwargs):
        return None


MAPPING = {
    "cards": {
        100: {"0": 1000, "1": 1001},
        200: {"0": 2000, "1": 2001},
    },
    "items": {10: 11, 20: 21},
    "idols": {1: 100, 2: 101},
    "customizations": {5: 50, 6: 60, 7: 70},
}


def _make_row(**overrides):
    row = {
        "UserMemoryId": "memory1",
        "CharacterId": 1,
        "IdolCardId": 1,
        "Vocal": 100,
        "Dance": 200,
        "Visual": 300,
        "Stamina": 400,
        "ExamBattleProduceCards": [
            {
                "Id": 100,
                "UpgradeCount": 0,
                "Customizes": [{"Id": 5, "CustomizeCount": 2}],
            },
            {
                "Id": 200,
                "UpgradeCount": 1,
                "Customizes": [
                    {"Id": 6, "CustomizeCount": 1},
                    {"Id": 7, "CustomizeCount": 3},
                ],
            },
        ],
        "ExamBattleProduceItemIds": [10, 20],
    }
    row.update(overrides)
    return row


class ConvertMemoryTests(unittest.TestCase):
    def test_idol_version_uses_idol_card_id(self):
        self.assertEqual(convert_memory(_make_row(CharacterId=999), MAPPING,
                         FakeCatalog())['p_idol_id'], 100)

    def test_missing_collection_is_not_inferred_empty(self):
        for field in ('ExamBattleProduceCards', 'ExamBattleProduceItemIds'):
            row = _make_row()
            del row[field]
            with self.assertRaises(ConvertError):
                convert_memory(row, MAPPING, FakeCatalog())

    def test_missing_customizations_is_not_inferred_empty(self):
        row = _make_row()
        del row['ExamBattleProduceCards'][0]['Customizes']
        with self.assertRaises(ConvertError):
            convert_memory(row, MAPPING, FakeCatalog())

    def test_two_native_ids_cannot_map_to_same_effect(self):
        import copy
        mapping = copy.deepcopy(MAPPING)
        mapping['customizations'][7] = mapping['customizations'][6]
        with self.assertRaises(ConvertError):
            convert_memory(_make_row(), mapping, FakeCatalog())

    def test_zero_count_is_rejected(self):
        row = _make_row()
        row['ExamBattleProduceCards'][0]['Customizes'][0]['CustomizeCount'] = 0
        with self.assertRaises(ConvertError):
            convert_memory(row, MAPPING, FakeCatalog())

    def test_lookup_and_shape(self):
        draft = convert_memory(_make_row(), MAPPING, FakeCatalog())
        self.assertEqual(draft["p_idol_id"], 100)
        self.assertEqual(
            draft["parameters"],
            {"vocal": 100, "dance": 200, "visual": 300, "stamina": 400},
        )
        self.assertEqual(draft["card_ids"], [1000, 2001, 0, 0, 0, 0])
        self.assertEqual(draft["item_ids"], [11, 21, 0, 0])
        self.assertEqual(
            draft["customizations"],
            ["50x2", "60x1e70x3", "", "", "", ""],
        )

    def test_card_and_customization_order_preserved(self):
        row = _make_row(
            ExamBattleProduceCards=[
                {
                    "Id": 200,
                    "UpgradeCount": 1,
                    "Customizes": [
                        {"Id": 7, "CustomizeCount": 1},
                        {"Id": 6, "CustomizeCount": 2},
                    ],
                },
                {"Id": 100, "UpgradeCount": 0, "Customizes": []},
            ],
        )
        draft = convert_memory(row, MAPPING, FakeCatalog())
        self.assertEqual(draft["card_ids"], [2001, 1000, 0, 0, 0, 0])
        self.assertEqual(
            draft["customizations"], ["70x1e60x2", "", "", "", "", ""]
        )

    def test_reject_bool_upgrade(self):
        row = _make_row(
            ExamBattleProduceCards=[
                {"Id": 100, "UpgradeCount": True, "Customizes": []},
            ],
        )
        with self.assertRaises(ConvertError):
            convert_memory(row, MAPPING, FakeCatalog())

    def test_reject_unknown_upgrade(self):
        row = _make_row(
            ExamBattleProduceCards=[
                {"Id": 100, "UpgradeCount": 5, "Customizes": []},
            ],
        )
        with self.assertRaises(ConvertError):
            convert_memory(row, MAPPING, FakeCatalog())

    def test_reject_unknown_card(self):
        row = _make_row(
            ExamBattleProduceCards=[
                {"Id": 999, "UpgradeCount": 0, "Customizes": []},
            ],
        )
        with self.assertRaises(ConvertError):
            convert_memory(row, MAPPING, FakeCatalog())

    def test_reject_unknown_customization(self):
        row = _make_row(
            ExamBattleProduceCards=[
                {
                    "Id": 100,
                    "UpgradeCount": 0,
                    "Customizes": [{"Id": 55, "CustomizeCount": 1}],
                },
            ],
        )
        with self.assertRaises(ConvertError):
            convert_memory(row, MAPPING, FakeCatalog())

    def test_reject_duplicate_customization(self):
        row = _make_row(
            ExamBattleProduceCards=[
                {
                    "Id": 100,
                    "UpgradeCount": 0,
                    "Customizes": [
                        {"Id": 5, "CustomizeCount": 1},
                        {"Id": 5, "CustomizeCount": 2},
                    ],
                },
            ],
        )
        with self.assertRaises(ConvertError):
            convert_memory(row, MAPPING, FakeCatalog())

    def test_reject_negative_stat(self):
        with self.assertRaises(ConvertError):
            convert_memory(_make_row(Vocal=-1), MAPPING, FakeCatalog())

    def test_reject_non_int_stat(self):
        with self.assertRaises(ConvertError):
            convert_memory(_make_row(Vocal="100"), MAPPING, FakeCatalog())

    def test_reject_too_many_cards(self):
        row = _make_row(
            ExamBattleProduceCards=[
                {"Id": 100, "UpgradeCount": 0, "Customizes": []},
            ]
            * 7,
        )
        with self.assertRaises(ConvertError):
            convert_memory(row, MAPPING, FakeCatalog())

    def test_reject_too_many_items(self):
        row = _make_row(ExamBattleProduceItemIds=[10, 20, 10, 20, 10])
        with self.assertRaises(ConvertError):
            convert_memory(row, MAPPING, FakeCatalog())

    def test_reject_empty_memory_id(self):
        with self.assertRaises(ConvertError):
            convert_memory(_make_row(UserMemoryId=""), MAPPING, FakeCatalog())


class ConvertSnapshotTests(unittest.TestCase):
    def test_produce_without_stage_payload_skipped_before_id_mapping(self):
        row = _make_row(UserMemoryId='produce', IdolCardId='not-in-stage-catalog',
                        ExamBattleProduceCards=[], ExamBattleProduceItemIds=[])
        result = convert_snapshot([row, _make_row()], MAPPING, FakeCatalog())
        self.assertEqual(len(result['memories']), 1)
        self.assertEqual(len(result['skipped']), 1)
        self.assertEqual(result['rejected'], [])

    def test_missing_stage_field_is_rejected_not_skipped(self):
        row = _make_row(ExamBattleProduceCards=[])
        del row['ExamBattleProduceItemIds']
        result = convert_snapshot([row], MAPPING, FakeCatalog())
        self.assertEqual(len(result['rejected']), 1)
        self.assertEqual(result['skipped'], [])

    def test_user_tag_does_not_define_mode(self):
        result = convert_snapshot([_make_row(MemoryTagId='memory_tag_13')], MAPPING, FakeCatalog())
        self.assertEqual(len(result['memories']), 1)
        self.assertEqual(result['skipped'], [])

    def test_catalog_runtime_errors_propagate(self):
        class BrokenCatalog(FakeCatalog):
            def validate_entities(self, *args):
                raise RuntimeError('broken catalog')
        with self.assertRaises(RuntimeError):
            convert_snapshot([_make_row()], MAPPING, BrokenCatalog())
    def test_snapshot_shape(self):
        rows = [_make_row(), _make_row(UserMemoryId="memory2", CharacterId=2)]
        result = convert_snapshot(rows, MAPPING, FakeCatalog())
        self.assertEqual(result["schema_version"], 1)
        self.assertEqual(len(result["memories"]), 2)
        self.assertEqual(result["memories"][0]["memory_id"], "memory1")
        self.assertEqual(result["memories"][1]["memory_id"], "memory2")
        self.assertIn("draft", result["memories"][0])
        self.assertEqual(result["rejected"], [])

    def test_duplicate_ids_are_fatal(self):
        rows = [_make_row(), _make_row()]
        with self.assertRaises(ConvertError):
            convert_snapshot(rows, MAPPING, FakeCatalog())

    def test_rejections_are_collected(self):
        rows = [_make_row(), _make_row(UserMemoryId="memory2", Vocal=-1)]
        result = convert_snapshot(rows, MAPPING, FakeCatalog())
        self.assertEqual(len(result["memories"]), 1)
        self.assertEqual(len(result["rejected"]), 1)
        rejected = result["rejected"][0]
        self.assertEqual(rejected["memory_id"], "memory2")
        self.assertIn("error", rejected)


if __name__ == "__main__":
    unittest.main()

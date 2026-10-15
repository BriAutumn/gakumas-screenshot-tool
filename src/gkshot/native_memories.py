"""Conversion of native gakuen memory rows into simulator drafts."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from gkshot.memory_library import validate_memory


MAX_CARDS = 6
MAX_ITEMS = 4
SCHEMA_VERSION = 1
CONVERSION_POLICY = 'stage-payload-v2'


class ConvertError(ValueError):
    """Raised when a native memory row cannot be converted."""


class NoStageMemory(ConvertError):
    """A valid native record with no competitive Stage payload."""


def require_stage_payload(row):
    # Missing/malformed collections indicate a broken export, not Produce.
    cards, items = row.get('ExamBattleProduceCards'), row.get('ExamBattleProduceItemIds')
    if not isinstance(cards, list) or not isinstance(items, list):
        raise ConvertError('舞台卡组和道具字段必须是列表，导出可能不完整')
    if not cards and not items:
        raise NoStageMemory('没有舞台卡组或道具（Produce / 无舞台数据），已跳过')


def _require_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConvertError(f"{name} must be an integer")
    if value < 0:
        raise ConvertError(f"{name} must be non-negative")
    return value


def _require_mapping(value: Any, name: str) -> Mapping:
    if not isinstance(value, Mapping):
        raise ConvertError(f"{name} must be a mapping")
    return value


def _lookup_id(section: Any, key: Any, kind: str) -> Any:
    section = _require_mapping(section, f"mapping.{kind}")
    if key not in section:
        raise ConvertError(f"unknown {kind} id: {key!r}")
    return section[key]


def _resolve_memory_id(row: Mapping) -> Any:
    memory_id = row.get("UserMemoryId")
    if not isinstance(memory_id, str) or not memory_id.strip():
        raise ConvertError("memory id must be non-empty")
    return memory_id


def _resolve_card(section: Any, card_id: Any, upgrade: int) -> Any:
    section = _require_mapping(section, "mapping.cards")
    if card_id not in section:
        raise ConvertError(f"unknown card id: {card_id!r}")
    variants = _require_mapping(
        section[card_id], f"mapping.cards[{card_id!r}]"
    )
    key = str(upgrade)
    if key not in variants:
        raise ConvertError(
            f"unknown upgrade count {upgrade} for card {card_id!r}"
        )
    return variants[key]


def _encode_customizations(customizes: Any, section: Any) -> str:
    if not isinstance(customizes, list):
        raise ConvertError("Customizes must be a list")
    section = _require_mapping(section, "mapping.customizations")
    parts = []
    seen = set()
    for entry in customizes:
        entry = _require_mapping(entry, "customization entry")
        native_id = entry.get("Id")
        count = _require_int(entry.get("CustomizeCount"), "CustomizeCount")
        if count == 0:
            raise ConvertError("CustomizeCount must be positive")
        if native_id not in section:
            raise ConvertError(f"unknown customization id: {native_id!r}")
        effect = _require_int(section[native_id], "mapped customization ID")
        if effect == 0 or effect in seen:
            raise ConvertError(f"invalid or duplicate customization effect: {effect!r}")
        seen.add(effect)
        parts.append(f"{effect}x{count}")
    return "e".join(parts)


def convert_memory(row, mapping, catalog):
    if not isinstance(row, Mapping):
        raise ConvertError("row must be a mapping")
    mapping = _require_mapping(mapping, "mapping")

    _resolve_memory_id(row)
    require_stage_payload(row)

    idol_id = _lookup_id(mapping.get("idols"), row.get("IdolCardId"), "idols")

    vocal = _require_int(row.get("Vocal"), "Vocal")
    dance = _require_int(row.get("Dance"), "Dance")
    visual = _require_int(row.get("Visual"), "Visual")
    stamina = _require_int(row.get("Stamina"), "Stamina")

    raw_cards = row.get("ExamBattleProduceCards")
    if not isinstance(raw_cards, list):
        raise ConvertError("ExamBattleProduceCards must be a list")
    if len(raw_cards) > MAX_CARDS:
        raise ConvertError(f"too many cards: {len(raw_cards)} > {MAX_CARDS}")

    card_ids = []
    customizations = []
    for card in raw_cards:
        card = _require_mapping(card, "card entry")
        upgrade = _require_int(card.get("UpgradeCount"), "UpgradeCount")
        card_ids.append(
            _resolve_card(mapping.get("cards"), card.get("Id"), upgrade)
        )
        customizations.append(
            _encode_customizations(
                card.get("Customizes"), mapping.get("customizations")
            )
        )

    while len(card_ids) < MAX_CARDS:
        card_ids.append(0)
        customizations.append("")

    raw_items = row.get("ExamBattleProduceItemIds")
    if not isinstance(raw_items, list):
        raise ConvertError("ExamBattleProduceItemIds must be a list")
    if len(raw_items) > MAX_ITEMS:
        raise ConvertError(f"too many items: {len(raw_items)} > {MAX_ITEMS}")

    item_ids = [
        _lookup_id(mapping.get("items"), item, "items") for item in raw_items
    ]
    while len(item_ids) < MAX_ITEMS:
        item_ids.append(0)

    draft = {
        "p_idol_id": idol_id,
        "parameters": {
            "vocal": vocal,
            "dance": dance,
            "visual": visual,
            "stamina": stamina,
        },
        "item_ids": item_ids,
        "card_ids": card_ids,
        "customizations": customizations,
    }
    return validate_memory(draft, catalog)


def convert_snapshot(rows, mapping, catalog):
    if isinstance(rows, (str, bytes)) or not isinstance(rows, Sequence):
        raise ConvertError("rows must be a sequence of mappings")

    seen = set()
    ids = []
    for row in rows:
        if not isinstance(row, Mapping):
            raise ConvertError("row must be a mapping")
        memory_id = _resolve_memory_id(row)
        if memory_id in seen:
            raise ConvertError(f"duplicate memory id: {memory_id!r}")
        seen.add(memory_id)
        ids.append(memory_id)

    memories = []
    rejected = []
    skipped = []
    for row, memory_id in zip(rows, ids):
        try:
            draft = convert_memory(row, mapping, catalog)
        except NoStageMemory as exc:
            skipped.append({'memory_id': memory_id, 'reason': str(exc)})
            continue
        except ValueError as exc:
            rejected.append({"memory_id": memory_id, "error": str(exc)})
            continue
        memories.append({"memory_id": memory_id, "draft": draft})

    return {
        "schema_version": SCHEMA_VERSION,
        "memories": memories,
        "rejected": rejected,
        "skipped": skipped,
        "conversion_policy": CONVERSION_POLICY,
    }

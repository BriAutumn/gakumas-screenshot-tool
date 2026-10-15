"""Versioned domain model; independent of recognition and simulation backends."""
import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path


def read_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"{path}: expected a JSON object")
    return value


def _integer(value, label: str, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{label}: expected integer >= {minimum}")
    return value


def _keys(value, expected: set[str], label: str):
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError(f"{label}: expected fields {', '.join(sorted(expected))}")


def _slots(value, count: int, label: str):
    if not isinstance(value, list) or len(value) != count:
        raise ValueError(f"{label}: expected {count} slots")
    return value


@dataclass(frozen=True)
class Parameters:
    vocal: int
    dance: int
    visual: int
    stamina: int


@dataclass(frozen=True)
class Loadout:
    schema_version: int
    stage_id: int
    support_bonus: float
    parameters: Parameters
    item_ids: tuple[int, ...]
    card_ids: tuple[tuple[int, ...], ...]
    customizations: tuple[tuple[str, ...], ...]
    p_idol_id: int | None = None

    @classmethod
    def from_dict(cls, value: dict) -> "Loadout":
        fields = {"schema_version", "stage_id", "support_bonus", "parameters",
                  "item_ids", "card_ids", "customizations"}
        _keys(value, fields | ({'p_idol_id'} if 'p_idol_id' in value else set()), 'loadout')
        p_idol_id = _integer(value['p_idol_id'], 'p_idol_id', 1) if 'p_idol_id' in value else None
        if _integer(value["schema_version"], "schema_version") != 1:
            raise ValueError("Unsupported schema_version; expected 1")
        stage = _integer(value["stage_id"], "stage_id", 1)
        bonus = value["support_bonus"]
        if type(bonus) not in (int, float) or not math.isfinite(bonus) or bonus < 0:
            raise ValueError("support_bonus: expected a finite nonnegative ratio (0.0472 = 4.72%)")
        params = value["parameters"]
        names = ("vocal", "dance", "visual", "stamina")
        _keys(params, set(names), "parameters")
        parameters = Parameters(**{k: _integer(params[k], k) for k in names})
        items = tuple(_integer(v, "item_ids") for v in _slots(value["item_ids"], 4, "item_ids"))
        cards = tuple(tuple(_integer(v, "card_ids") for v in _slots(row, 6, "card row"))
                      for row in _slots(value["card_ids"], 2, "card_ids"))
        customizations = []
        for row in _slots(value["customizations"], 2, "customizations"):
            for token in _slots(row, 6, "customization row"):
                if not isinstance(token, str) or any(c in token for c in "-_\r\n"):
                    raise ValueError("customization: expected a token without slot delimiters")
            customizations.append(tuple(row))
        return cls(1, stage, bonus, parameters, items, cards, tuple(customizations), p_idol_id)

    def to_dict(self) -> dict:
        # Convert tuple storage to JSON arrays, including nested rows.
        value = asdict(self)
        if self.p_idol_id is None:
            del value['p_idol_id']
        return json.loads(json.dumps(value, allow_nan=False))

    @classmethod
    def load(cls, path: Path) -> "Loadout":
        return cls.from_dict(read_json(path))

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from gkshot.models import Loadout


@dataclass(frozen=True)
class RecognitionResult:
    loadout: Loadout
    source: str
    image_sha256: str
    warnings: tuple[str, ...] = ()


class Recognizer(Protocol):
    def recognize(self, screenshot: Path) -> RecognitionResult:
        """Return a complete loadout; uncertain or missing fields must not be guessed."""
        ...

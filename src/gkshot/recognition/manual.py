"""Explicit manual annotations for pipeline development, not image recognition."""
import hashlib
from pathlib import Path

from gkshot.models import Loadout, read_json
from gkshot.recognition.base import RecognitionResult


class ManualAnnotationRecognizer:
    def __init__(self, annotations: Path):
        self.annotations = annotations

    def recognize(self, screenshot: Path) -> RecognitionResult:
        content = screenshot.read_bytes()
        if not content:
            raise ValueError("Screenshot file is empty")
        digest = hashlib.sha256(content).hexdigest()
        data = read_json(self.annotations)
        if data.get("image_sha256") != digest:
            raise ValueError("Annotation image_sha256 does not match the screenshot")
        if "loadout" not in data:
            raise ValueError("Annotation must contain loadout")
        return RecognitionResult(
            Loadout.from_dict(data["loadout"]), "manual-annotation", digest,
            ("Manual annotations were used; OCR and icon matching have not run.",),
        )

from dataclasses import dataclass
from pathlib import Path

from gkshot.adapters.gktools import GKToolsAdapter
from gkshot.recognition.base import RecognitionResult, Recognizer


@dataclass(frozen=True)
class PipelineResult:
    recognition: RecognitionResult
    url: str


def run(screenshot: Path, recognizer: Recognizer, adapter: GKToolsAdapter) -> PipelineResult:
    recognition = recognizer.recognize(screenshot)
    return PipelineResult(recognition, adapter.to_url(recognition.loadout))

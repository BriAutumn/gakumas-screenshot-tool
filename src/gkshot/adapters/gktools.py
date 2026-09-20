from urllib.parse import urlencode, urlsplit

from gkshot.models import Loadout

DEFAULT_BASE_URL = "https://gktools.ris.moe/zh-Hans/simulator"


class GKToolsAdapter:
    def __init__(self, base_url: str = DEFAULT_BASE_URL):
        parts = urlsplit(base_url)
        if parts.scheme != "https" or not parts.netloc or parts.query or parts.fragment:
            raise ValueError("base_url must be an HTTPS URL without query or fragment")
        self.base_url = base_url

    def to_url(self, loadout: Loadout) -> str:
        loadout = Loadout.from_dict(loadout.to_dict())
        p = loadout.parameters
        query = {
            "stage": str(loadout.stage_id),
            "support_bonus": str(loadout.support_bonus),
            "params": "-".join(map(str, (p.vocal, p.dance, p.visual, p.stamina))),
            "items": "-".join(map(str, loadout.item_ids)),
            "cards": "_".join("-".join(map(str, row)) for row in loadout.card_ids),
            "customizations": "_".join("-".join(row) for row in loadout.customizations),
        }
        return f"{self.base_url}?{urlencode(query)}"

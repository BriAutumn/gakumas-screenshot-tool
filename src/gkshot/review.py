"""Only an explicitly reviewed, complete draft can cross into URL export."""
from gkshot.models import Loadout


def reviewed_loadout(draft: dict, *, confirmed: bool, catalog) -> Loadout:
    if not confirmed:
        raise ValueError("请先核对属性、道具、强化和自定义效果，再勾选确认。")
    if any(token is None for row in draft["customizations"] for token in row):
        raise ValueError("还有未核对的自定义效果。请设置效果，或确认其余卡片无自定义。")
    if draft["stage_id"] is None or draft["support_bonus"] is None:
        raise ValueError("请填写场地和支援加成；这两项无法从截图读取。")
    result = Loadout.from_dict(draft)
    catalog.validate_loadout(result)
    return result

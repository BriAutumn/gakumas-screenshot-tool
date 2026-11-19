"""Contest skill-card switches, resolved against the active simulator catalog."""

from itertools import product
from unicodedata import normalize


# PLv 51–90 switch table: https://seesaawiki.jp/gakumasu/d/PLv
# Names keep this usable across catalog revisions and cover both upgrade levels.
SWITCH_PAIRS = (
    ('バランス感覚', '軌道修正'),
    ('今日もおはよう', 'おまもりミラクル'),
    ('一歩', '形勢逆転'),
    ('愛嬌', 'パンプアップ'),
    ('もう少しだけ', 'がむしゃら'),
    ('ラッキー♪', 'ノンストップ'),
    ('眼力', '一発勝負'),
    ('幸せな時間', '冒険心'),
    ('せーのっ！', 'フルスロットル'),
    ('決めポーズ', 'スリリング'),
    ('ハートの合図', '気まぐれハート'),
    ('汗と成長', 'リスキーチャンス'),
    ('スタートダッシュ', '大胆不敵'),
    ('あふれる思い出', '成長痛'),
    ('はじけるパッション', 'オーバードライブ'),
    ('コール＆レスポンス', '脚光'),
    ('開花', 'せのびの魔法'),
    ('総合芸術', 'エンターテイナー'),
    ('バズワード', '話題沸騰'),
    ('200％スマイル', 'びしっとキメ顔'),
    ('翔び立て！', '羽ばたけ！'),
    ('楽観的', '競争原理'),
    ('深呼吸', '安定感'),
    ('アドリブ', '再始動'),
    ('情熱ターン', '衝動'),
    ('パステル気分', 'メイクアップ'),
    ('励まし', 'ロマンチック'),
    ('ふれあい', '天使の微笑み'),
    ('ありがとうの言葉', 'ごきげんステップ'),
    ('アッチェレランド', 'ラッシュ'),
    ('オープニングアクト', 'ブースト'),
)


def _family(name):
    return normalize('NFKC', name).removesuffix('+')


def switch_families(skill_cards):
    families = {}
    for card_id, card in skill_cards.items():
        families.setdefault(_family(card['name']), set()).add(card_id)
    resolved = [(families.get(_family(a)), families.get(_family(b))) for a, b in SWITCH_PAIRS]
    return tuple((a, b) for a, b in resolved if a and b)


def switch_conflicts(card_ids, families):
    present = {card for group in card_ids for card in group}
    return [(a & present, b & present) for a, b in families if a & present and b & present]


def legal_variants(loadout, families, skill_cards):
    conflicts = switch_conflicts(loadout['card_ids'], families)
    if not conflicts:
        yield loadout, []
        return
    conflicted_cards = set().union(*(a | b for a, b in conflicts))
    for kept_sides in product(*conflicts):
        removed = conflicted_cards - set().union(*kept_sides)
        cards = [[card if card not in removed else 0 for card in group]
                 for group in loadout['card_ids']]
        customizations = [[token if card else '' for card, token in zip(group, tokens)]
                          for group, tokens in zip(cards, loadout['customizations'])]
        choices = []
        for (a, b), selected in zip(conflicts, kept_sides):
            choices.append({
                'kept': [{'id': card, 'name': skill_cards[card]['name']} for card in sorted(selected)],
                'excluded': [{'id': card, 'name': skill_cards[card]['name']}
                             for card in sorted((a | b) - selected)],
            })
        yield dict(loadout, card_ids=cards, customizations=customizations), choices

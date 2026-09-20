"""Visual kana equivalence for names, with conservative art-backed OCR repair."""
import re
import unicodedata
from difflib import SequenceMatcher


def canonical_name(text):
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', text or '')).translate(str.maketrans('ヘベペ', 'へべぺ'))


def art_backed_name_matches(observed, expected, catalog):
    observed=canonical_name(observed).rstrip('+')
    expected=canonical_name(expected).rstrip('+')
    if observed==expected:return True
    families={canonical_name(c['name']).rstrip('+') for c in catalog.skill_cards.values()}
    if observed in families or len(observed)<4 or len(observed)!=len(expected):return False
    if sum(a!=b for a,b in zip(observed,expected))!=1:return False
    ranked=sorted(((SequenceMatcher(None,observed,name).ratio(),name) for name in families),reverse=True)
    return ranked[0][1]==expected and ranked[0][0]>=.75 and ranked[0][0]-ranked[1][0]>=.15

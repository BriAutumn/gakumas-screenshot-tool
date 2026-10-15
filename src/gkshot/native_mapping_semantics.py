"""Conservative effect signatures; unsupported effects remain explicit candidates."""
from decimal import Decimal, InvalidOperation
import re

GROW = {'LessonAdd':'score', 'BlockAdd':'genki', 'FullPowerPointAdd':'fullPowerCharge',
    'ReviewAdd':'goodImpressionTurns', 'AggressiveAdd':'motivation', 'LessonBuffAdd':'concentration',
    'ParameterBuffTurnAdd':'goodConditionTurns', 'ParameterBuffMultiplePerTurnAdd':'perfectConditionTurns',
    'StaminaConsumptionDownTurnAdd':'halfCostTurns', 'LessonCountAdd':'scoreTimes', 'CostReduce':'cost'}
DEPEND = {'LessonDependBlockAdd':'scoreByGenki', 'LessonDependExamReviewAdd':'scoreByGoodImpressionTurns',
          'LessonDependExamCardPlayAggressiveAdd':'scoreByMotivation'}
TYPED = {'CostPenetrateReduce':'stamina', 'CostAggressiveReduce':'motivation',
    'CostFullPowerPointReduce':'fullPowerCharge', 'CostLessonBuffReduce':'concentration',
    'CostParameterBuffReduce':'goodConditionTurns', 'CostReviewReduce':'goodImpressionTurns',
    'CostParameterBuffMultiplePerTurnReduce':'perfectConditionTurns'}
EXAM = {'ExamBlock':'genki', 'ExamLesson':'score', 'ExamReview':'goodImpressionTurns',
    'ExamCardPlayAggressive':'motivation', 'ExamFullPowerPoint':'fullPowerCharge', 'ExamLessonBuff':'concentration'}
TIMED = {'ExamParameterBuff':'goodConditionTurns', 'ExamParameterBuffMultiplePerTurn':'perfectConditionTurns',
         'ExamStaminaConsumptionDown':'halfCostTurns'}

def number(value, divisor=1):
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal, str)):
        raise ValueError('not a number')
    try:
        d = Decimal(str(value)) / Decimal(divisor)
    except (InvalidOperation, ZeroDivisionError) as error:
        raise ValueError('invalid number') from error
    if not d.is_finite():
        raise ValueError('nonfinite')
    return format(d.normalize(), 'f') if d else '0'

def neutral(v):
    return v in (None, '', 0, [], {}) or (isinstance(v, str) and v.endswith('_Unknown'))

def simple_fields(row, allowed, ignored=()):
    return all(k in allowed or k in ignored or neutral(v) for k, v in row.items())

def native_atom(g):
    if not isinstance(g, dict):
        return None
    if g.get('effectType') in ('ProduceCardGrowEffectType_PlayEffectTriggerChange',
                             'ProduceCardGrowEffectType_PlayTriggerChange'):
        usable = g['effectType'].endswith('_PlayTriggerChange')
        if not neutral(g.get('playEffectProduceExamTriggerId' if usable else 'playProduceExamTriggerId')):
            return None
        if not simple_fields(g, {'effectType','playEffectProduceExamTriggerId',
                'playProduceExamTriggerId',
                'targetPlayEffectProduceExamTriggerIds','resolvedTrigger','resolvedTargetTriggers'}, {'effectGroupIds'}):
            return None
        new, old = g.get('resolvedTrigger'), g.get('resolvedTargetTriggers', [])
        if not isinstance(new, dict) or len(old) != 1:
            return None
        def predicate(t):
            if not simple_fields(t, {'phaseTypes','fieldStatusTypes','fieldStatusValues'},
                    {'id','produceDescriptions','playProduceDescriptions','playEffectProduceDescriptions'}):
                return None
            if t.get('phaseTypes') != ['ProduceExamPhaseType_None'] or len(t.get('fieldStatusTypes', [])) != 1 or len(t.get('fieldStatusValues', [])) != 1:
                return None
            key = {'ProduceExamFieldStatusType_ParameterBuffUp':'goodConditionTurns',
                   'ProduceExamFieldStatusType_LessonBuffUp':'concentration',
                   'ProduceExamFieldStatusType_ReviewUp':'goodImpressionTurns'}.get(t['fieldStatusTypes'][0])
            return (key, number(t['fieldStatusValues'][0])) if key else None
        before, after = predicate(old[0]), predicate(new)
        if before and after and before[0] == after[0]:
            return ('usableCondition' if usable else 'condition', after[0], after[1])
        return None
    if not simple_fields(g, {'effectType','value','examEffect','playMovePositionType'},
                         {'effectGroupIds','playProduceExamEffectId'}):
        return None
    t = g.get('effectType', '').removeprefix('ProduceCardGrowEffectType_')
    if t in GROW:
        return ('grow', GROW[t], number(g.get('value', 0)))
    if t in DEPEND:
        return ('grow', DEPEND[t], number(g.get('value', 0), 1000))
    if t in TYPED:
        return ('grow', 'typedCost:' + TYPED[t], number(g.get('value', 0)))
    if t == 'InitialAdd':
        return ('meta', 'forceInitialHand', '1')
    if t == 'PlayMovePositionTypeChange' and g.get('playMovePositionType') == 'ProduceCardMovePositionType_Grave':
        return ('meta', 'limit', '0')
    if t != 'EffectAdd' or not isinstance(g.get('examEffect'), dict):
        return None
    e = g['examEffect']
    t = e.get('effectType', '').removeprefix('ProduceExamEffectType_')
    if t == 'ExamEffectTimer':
        if not simple_fields(e, {'effectType','effectValue1','effectCount',
                'chainProduceExamEffectId','resolvedChainEffect'}, {'effectGroupIds'}):
            return None
        if type(e.get('effectValue1')) is not int or e['effectValue1'] != 1 or type(e.get('effectCount')) is not int or e['effectCount'] != 1:
            return None
        inner = e.get('resolvedChainEffect')
        if not isinstance(inner, dict) or inner.get('effectType') != 'ProduceExamEffectType_ExamPreservation':
            return None
        atom = native_atom({'effectType':'ProduceCardGrowEffectType_EffectAdd','examEffect':inner})
        return ('nextTurn', 'stance:preservation', '1') if atom == ('action', 'stance:preservation', '1') else None
    if t == 'ExamCardUpgrade':
        if (simple_fields(e, {'effectType','produceCardSearchId','pickRangeType'}, {'effectGroupIds'})
                and e.get('produceCardSearchId') == 'p_card_search-hand'
                and e.get('pickRangeType') == 'ProducePickRangeType_All'):
            return ('action', 'upgradeHand', '1')
        return None
    if t == 'ExamCardMove':
        if not simple_fields(e, {'effectType','produceCardSearchId','pickRangeType',
                'movePositionType','pickCountMin','pickCountMax'}, {'effectGroupIds'}):
            return None
        if (e.get('produceCardSearchId') == 'p_card_search-deck'
                and e.get('pickRangeType') == 'ProducePickRangeType_Select'
                and e.get('movePositionType') == 'ProduceCardMovePositionType_Hold'
                and type(e.get('pickCountMin')) is int and e['pickCountMin'] == 1
                and type(e.get('pickCountMax')) is int and e['pickCountMax'] == 1):
            return ('action', 'holdSelected:deck', '1')
        return None
    if t == 'ExamAddGrowEffect':
        if not simple_fields(e, {'effectType','produceCardSearchId','produceCardGrowEffectIds','resolvedGrowEffects','pickRangeType'}, {'effectGroupIds'}):
            return None
        if e.get('pickRangeType') != 'ProducePickRangeType_All':
            return None
        target = {'p_card_search-hand':'hand', 'p_card_search-hold':'held',
                  'p_card_search-deck_all':'all'}.get(e.get('produceCardSearchId'))
        inner = e.get('resolvedGrowEffects', [])
        if not target or len(inner) != 1:
            return None
        child = dict(inner[0])
        child.pop('id', None)
        atom = native_atom(child)
        if atom is None or atom[:2] != ('grow', 'score'):
            return None
        return ('action', 'grow:' + target + ':score', atom[2])
    if not simple_fields(e, {'effectType','effectValue1','effectCount','effectTurn'}, {'effectGroupIds'}):
        return None
    t = e.get('effectType', '').removeprefix('ProduceExamEffectType_')
    count, turn = e.get('effectCount', 0), e.get('effectTurn', 0)
    if isinstance(count, bool) or isinstance(turn, bool):
        return None
    if t in TIMED and count in (0, 1):
        return ('action', TIMED[t], number(turn))
    if turn != 0:
        return None
    if t == 'ExamPlayableValueAdd':
        return ('action', 'cardUsesRemaining', number(count))
    if count not in (0, 1):
        return None
    if t in EXAM:
        return ('action', EXAM[t], number(e.get('effectValue1', 0)))
    if t == 'ExamExtraTurn':
        return ('action', 'turnsRemaining', '1')
    if t == 'ExamPreservation' and e.get('effectValue1') == 1:
        return ('action', 'stance:preservation', '1')
    if t == 'ExamConcentration' and e.get('effectValue1') == 1:
        return ('action', 'stance:strength', '1')
    if t == 'ExamLessonDependBlock':
        return ('action', 'scoreByGenki', number(e.get('effectValue1', 0), 1000))
    if t in ('ExamLessonDependExamReview','ExamLessonDependExamCardPlayAggressive'):
        key = 'scoreByGoodImpressionTurns' if t == 'ExamLessonDependExamReview' else 'scoreByMotivation'
        return ('action', key, number(e.get('effectValue1', 0), 1000))
    return None

def native_signature(group):
    try:
        levels = group['levels']
        if not isinstance(levels, list) or not levels:
            return None
        rows = []
        for r in levels:
            level = r['level']
            if type(level) is not int:
                return None
            atoms = [native_atom(g) for g in r['effects']]
            if not atoms or None in atoms or len(set(atoms)) != len(atoms):
                return None
            rows.append((level, tuple(sorted(atoms))))
        rows.sort()
        return tuple(rows) if [r[0] for r in rows] == list(range(1, len(rows) + 1)) else None
    except (KeyError, TypeError, ValueError, AttributeError):
        return None

NUM = r'([+-]?\d+(?:\.\d+)?)'
BODY = r'at:prestage\s*\{\s*target:this\s*\{\s*g\.([A-Za-z]+)\+=' + NUM + r'\s*\}\s*\}'
LEVEL = re.compile(r'\s*level:(\d+)\s*\{\s*' + BODY + r'\s*\}')

def simulator_signature(row):
    try:
        if row.get('cost'):
            return None
        effects, actions = row.get('effects', ''), row.get('actions', '')
        if not isinstance(effects, str) or not isinstance(actions, str):
            return None
        effects, actions = effects.strip(), actions.strip().removesuffix(';').strip()
        if row.get('conditions'):
            m = re.fullmatch(r'@usable\s+if:([A-Za-z]+)>=' + NUM, row['conditions'].strip())
            if (m and not effects and not actions and row.get('max', 1) == 1
                    and not row.get('forceInitialHand') and neutral(row.get('limit'))):
                return ((1, (('usableCondition', m[1], number(m[2])),)),)
            return None
        if effects and actions:
            return None
        if row.get('forceInitialHand') is True and not effects and not actions:
            return ((1, (('meta', 'forceInitialHand', '1'),)),)
        if type(row.get('limit')) is int and row['limit'] == 0 and not effects and not actions:
            return ((1, (('meta', 'limit', '0'),)),)
        if row.get('forceInitialHand') or not neutral(row.get('limit')):
            return None
        def grow(key, value):
            if key == 'typedCost':
                alias = row.get('alias', '')
                keys = [k for k in TYPED.values() if alias.startswith(k + 'Cost')]
                if alias.startswith('perfectConditionCost'):
                    keys = ['perfectConditionTurns']
                if len(keys) != 1:
                    raise ValueError('unknown typed cost')
                key = 'typedCost:' + keys[0]
            return ('grow', key, number(value))
        if effects:
            m = re.fullmatch(BODY, effects)
            if m:
                result = ((1, (grow(*m.groups()),)),)
            else:
                parts, pos = [], 0
                while pos < len(effects):
                    m = LEVEL.match(effects, pos)
                    if not m:
                        return None
                    parts.append((int(m[1]), (grow(m[2], m[3]),)))
                    pos = m.end()
                parts.sort()
                result = tuple(parts)
        elif actions:
            if actions == 'at:turn { setStance(preservation); limit:1 }':
                return ((1, (('nextTurn', 'stance:preservation', '1'),)),) if row.get('max', 1) == 1 else None
            if actions in ('upgradeHand', 'holdSelected[deck]'):
                key = 'upgradeHand' if actions == 'upgradeHand' else 'holdSelected:deck'
                return ((1, (('action', key, '1'),)),) if row.get('max', 1) == 1 else None
            m = re.fullmatch(r'@(trigger|goodLine|concLine)\s+if:([A-Za-z]+)>=' + NUM, actions)
            if m:
                return ((1, (('condition', m[2], number(m[3])),)),) if row.get('max', 1) == 1 else None
            if actions.startswith('level:'):
                pattern = re.compile(r'\s*level:(\d+)\s*\{\s*target:(hand|held|all)\s*\{\s*g\.score\+=' + NUM + r'\s*\}\s*\}')
                parts, pos = [], 0
                while pos < len(actions):
                    m = pattern.match(actions, pos)
                    if not m:
                        return None
                    parts.append((int(m[1]), (('action', 'grow:' + m[2] + ':score', number(m[3])),)))
                    pos = m.end()
                parts.sort()
                return tuple(parts) if [p[0] for p in parts] == list(range(1, row.get('max', 1) + 1)) else None
            m = re.fullmatch(r'target:(hand|held|all)\s*\{\s*g\.score\+=' + NUM + r'\s*\}', actions)
            if m:
                return ((1, (('action', 'grow:' + m[1] + ':score', number(m[2])),)),) if row.get('max', 1) == 1 else None
            if actions == 'setStance(preservation)':
                return ((1, (('action', 'stance:preservation', '1'),)),) if row.get('max', 1) == 1 else None
            if actions == 'setStance(strength)':
                return ((1, (('action', 'stance:strength', '1'),)),) if row.get('max', 1) == 1 else None
            m = re.fullmatch(r'([A-Za-z]+)\+=' + NUM, actions)
            if m:
                result = ((1, (('action', m[1], number(m[2])),)),)
            else:
                m = re.fullmatch(r'score\+=(genki|goodImpressionTurns|motivation)\*' + NUM, actions)
                if not m:
                    return None
                key = {'genki':'scoreByGenki', 'goodImpressionTurns':'scoreByGoodImpressionTurns',
                       'motivation':'scoreByMotivation'}[m[1]]
                result = ((1, (('action', key, number(m[2])),)),)
        else:
            return None
        if [r[0] for r in result] != list(range(1, row.get('max', 1) + 1)):
            return None
        return result
    except (KeyError, TypeError, ValueError, AttributeError):
        return None

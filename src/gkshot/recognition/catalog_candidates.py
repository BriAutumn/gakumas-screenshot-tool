"""Catalog-derived candidates and a deliberately bounded game-text renderer.

The upstream parser supplies the effect tree. No card IDs or card-specific
sentences occur here. Unknown syntax rejects the entire card's generated rule.
"""
import copy
import hashlib
import itertools
import json
import re
from decimal import Decimal
from pathlib import Path

from gkshot.catalog import ASSETS

FIELDS=('conditions','cost','actions','effects')
PATCHABLE={'phase','filter','conditions','targets','actions','effects','limit','ttl','delay','group'}
LABELS={'score':'スコア','genki':'元気','motivation':'やる気','concentration':'集中',
        'goodImpressionTurns':'好印象','goodConditionTurns':'好調',
        'perfectConditionTurns':'絶好調','halfCostTurns':'消費体力減少',
        'fullPowerCharge':'全力値','enthusiasm':'熱意','stamina':'体力',
        'fixedStamina':'固定体力','doubleCostTurns':'消費体力増加'}
TURNS={'goodConditionTurns','perfectConditionTurns','halfCostTurns','doubleCostTurns'}

class Unsupported(ValueError):
    pass

def number(expr):
    if set(expr)!= {'type','value'} or expr['type']!='number':raise Unsupported('Nonconstant expression')
    return Decimal(str(expr['value']))

def fmt(n):
    return format(Decimal(n).normalize(),'f')

def load_tree(root=ASSETS):
    manifest=json.loads((root/'manifest.json').read_text(encoding='utf8'))
    raw=(root/'catalog-effects.json').read_bytes()
    if hashlib.sha256(raw).hexdigest()!=manifest['files']['catalog-effects.json']['sha256']:
        raise ValueError('Parsed catalog hash mismatch')
    tree=json.loads(raw)
    for name,digest in tree['source_sha256'].items():
        if hashlib.sha256((root/(name+'.json')).read_bytes()).hexdigest()!=digest:
            raise ValueError('Parsed catalog source changed')
    return tree

def candidates(card_id,catalog,tree):
    card=catalog.skill_cards[card_id]
    ids=[int(i) for i in card['availableCustomizations'].split(',') if i]
    for levels in itertools.product(*(range(catalog.customizations[i]['max']+1) for i in ids)):
        data=copy.deepcopy(tree['cards'][str(card_id)])
        data.update(initial=bool(card['forceInitialHand']),limit=card['limit'],unique=card['unique'])
        for effect_id,level in zip(ids,levels):
            if not level:continue
            raw=catalog.customizations[effect_id]
            if raw['forceInitialHand']:data['initial']=True
            if raw['limit']!='':data['limit']=raw['limit']
            for field in FIELDS:
                for patch in tree['customizations'][str(effect_id)][field]:
                    if patch.get('level',1)!=level:continue
                    if patch['op']=='append':data[field].append(copy.deepcopy(patch['effect']))
                    elif patch['op']=='patch':
                        hits=[e for e in data[field] if e.get('anchor')==patch['anchor']]
                        if len(hits)!=1 or set(patch['delta'])-PATCHABLE:
                            raise Unsupported('Unresolvable customization anchor')
                        hits[0].update(copy.deepcopy(patch['delta']))
                    else:raise Unsupported('Unknown patch operation')
        token='e'.join(f'{i}x{v}' for i,v in zip(ids,levels) if v)
        yield {'token':token,'effects':data}

def growth(data):
    out={}
    for row in data['effects']:
        if set(row)-{'phase','targets','actions'} or row.get('phase')!='prestage' or row.get('targets')!=['this']:
            raise Unsupported('Dynamic or non-self growth')
        for a in row.get('actions',[]):
            if set(a)!={'type','lhs','op','rhs'} or a['type']!='assignment' or a['op']!='+=' or not a['lhs'].startswith('g.'):
                raise Unsupported('Unsupported growth operation')
            key=a['lhs'][2:];out[key]=out.get(key,Decimal(0))+number(a['rhs'])
    allowed=set(LABELS)|{'typedCost','cost','scoreByGoodImpressionTurns','scoreByGenki','stanceLevel'}
    if set(out)-allowed:raise Unsupported('Unsupported growth field: '+str(set(out)-allowed))
    return out

def condition(expr):
    if expr=={'type':'identifier','name':'isFullPower'}:return '全力の場合、?'
    if expr=={'type':'comparison','op':'!=','left':{'type':'identifier','name':'stance'},'right':{'type':'identifier','name':'none'}}:
        return 'いずれかの指針の場合、?'
    if set(expr)!={'type','op','left','right'} or expr['type']!='comparison' or expr['op']!='>=':
        raise Unsupported('Unsupported condition')
    left=expr['left']
    if left.get('type')!='identifier' or left.get('name') not in LABELS:raise Unsupported('Unsupported condition field')
    if left['name'] in ('goodConditionTurns','perfectConditionTurns') and number(expr['right'])==1:
        return LABELS[left['name']]+'状態の場合、?'
    unit='ターン' if left['name'] in TURNS else ''
    return LABELS[left['name']]+'が'+fmt(number(expr['right']))+unit+'以上の場合、?'

def action(a,g):
    if a.get('type')=='assignment':
        if set(a)!={'type','lhs','op','rhs'}:raise Unsupported('Extra assignment semantics')
        key=a['lhs'];op=a['op'];rhs=a['rhs']
        if key=='cardUsesRemaining' and op=='+=':return r'スキルカード使用数追加\+'+fmt(number(rhs))
        if key not in LABELS:raise Unsupported('Unknown action field '+key)
        if op=='*=':return LABELS[key]+re.escape(fmt(number(rhs)))+'倍'
        if op!='+=':raise Unsupported('Unsupported action operator')
        if key=='score' and rhs.get('type')=='binary':
            if rhs.get('op')!='*' or rhs.get('left',{}).get('type')!='identifier':raise Unsupported('Complex score')
            source=rhs['left']['name'];field={'goodImpressionTurns':'scoreByGoodImpressionTurns','genki':'scoreByGenki'}.get(source)
            if not field or g.get('score'):raise Unsupported('Mixed score expression')
            percent=(number(rhs['right'])+g.get(field,0))*100
            return LABELS[source]+'の'+fmt(percent)+r'%分スコア上昇'
        value=number(rhs)+g.get(key,0)
        if key in ('stamina','fixedStamina'):return '体力回復'+fmt(value)
        if key in TURNS:return LABELS[key]+fmt(value)+'ターン'
        return LABELS[key]+r'[+:]'+fmt(value)
    if a.get('type')=='identifier':
        if set(a)!={'type','name'}:raise Unsupported('Extra identifier semantics')
        if a['name']=='drawCard':return 'スキルカードを引く'
        if a['name']=='upgradeHand':return '手札をすべて試験・ステージ中強化'
        raise Unsupported('Unknown bare action')
    if a.get('type')=='call':
        if set(a)!={'type','name','target','args'} or a['target'] is not None:raise Unsupported('Targeted call')
        args=a['args'];name=a['name']
        if name=='setScoreBuff' and len(args)==1:
            return r'(?:スコア|パラメータ)上昇量増加'+fmt(number(args[0])*100)+'%'
        if name=='setGoodImpressionTurnsEffectBuff' and len(args)==1:
            return r'好印象強化\+'+fmt(number(args[0])*100)+'%'
        if name=='setStance' and len(args)==1 and args[0].get('type')=='identifier':
            stance=args[0]['name'];base={'strength':('強気',1),'strength2':('強気',2),'preservation':('温存',1),'preservation2':('温存',2)}
            if stance not in base:raise Unsupported('Unknown stance')
            label,level=base[stance];level+=g.get('stanceLevel',0)
            return label+('' if level==1 else fmt(level)+'段階目')+'に変更'
        if name=='drawCard' and len(args)==1:return 'スキルカードを'+fmt(number(args[0]))+'枚引く'
    raise Unsupported('Unsupported action')

def effects(rows,g):
    text=''
    for row in rows:
        if set(row)-{'anchor','conditions','actions','effects','phase','limit'}:raise Unsupported('Unhandled effect attributes')
        text+=''.join(condition(c) for c in row.get('conditions',[]))
        phase=row.get('phase')
        if phase=='startOfTurn' and row.get('limit'):
            text+='以降の'+fmt(row['limit'])+'ターンの間、?ターン開始時、?'
        elif phase=='turn' and row.get('limit')==1:text+=r'[トヘ・]?次のターン、?'
        elif phase=='cardUsed' and not row.get('limit'):text+='以降、?スキルカード使用時、?'
        elif phase or row.get('limit'):raise Unsupported('Unsupported trigger or limit')
        actions=row.get('actions',[])
        # Repeated equal score assignments are displayed once with an explicit
        # repetition count. Other repeats retain their separate effect lines.
        rendered=[]
        for a,group in itertools.groupby(actions):
            count=len(list(group))
            if (count>1 and a.get('type')=='assignment' and a.get('lhs')=='score' and a.get('op')=='+='):
                rendered.append(action(a,g)+r'\('+str(count)+r'回\)')
            else:rendered.extend([action(a,g)]*count)
        text+='・?'.join(rendered)
        text+=effects(row.get('effects',[]),g)
    return text

def render(card,data):
    usable=''
    for row in data['conditions']:
        if set(row)-{'conditions','anchor'} or len(row.get('conditions',[]))!=1:
            raise Unsupported('Unsupported usability conditions')
        usable+=condition(row['conditions'][0])+'使用可'
    g=growth(data);cost='';corner=None
    for row in data['cost']:
        if set(row)!={'actions'}:raise Unsupported('Conditional cost')
        for a in row['actions']:
            if set(a)!={'type','lhs','op','rhs'} or a['type']!='assignment' or a['op']!='-=':raise Unsupported('Unsupported cost')
            key=a['lhs'];value=number(a['rhs'])
            if key=='cost':
                value=max(Decimal(0),value-g.get('cost',0))
                # Fixed costs need not be read to distinguish states. Changed
                # hidden cost is required below on EVERY candidate for this card.
                corner=int(value)
            else:
                if key not in LABELS:raise Unsupported('Unknown typed cost')
                value=max(Decimal(0),value-g.get('typedCost',0))
                if value:cost+=(r'全力(?:値)?' if key=='fullPowerCharge' else LABELS[key])+'消費'+fmt(value)
    if data['limit'] not in ('',0,1):raise Unsupported('Unsupported card limit')
    result=re.escape(card['name'])+('試験・ステージ開始時手札に入る' if data['initial'] else '')+cost+usable+effects(data['actions'],g)
    result+=('重複不可' if data['unique'] else '')+('試験・ステージ中1回' if data['limit']==1 else '')
    return {'pattern':result,'hidden_cost':corner}

def compile_card(card_id,catalog,tree):
    """All states or none: unsupported states must never be silently omitted."""
    card=catalog.skill_cards[card_id];cases=[]
    for candidate in candidates(card_id,catalog,tree):
        case=render(card,candidate['effects']);case['token']=candidate['token'];cases.append(case)
    costs={c['hidden_cost'] for c in cases}
    for case in cases:
        value=case.pop('hidden_cost')
        if len(costs)>1:
            if value is None or not 1<=value<=9:raise Unsupported('Unsupported corner cost evidence')
            case['corner_cost']=value
    signatures=[(c['pattern'],c.get('corner_cost')) for c in cases]
    if len(signatures)!=len(set(signatures)):raise Unsupported('Indistinguishable candidate states')
    return cases

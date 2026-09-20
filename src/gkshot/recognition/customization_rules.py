"""Compile small, catalog-bound effect axes into complete tooltip states.

This is deliberately not a general interpreter for the game's effect language.
Only a whole, supported numeric modifier is accepted. Complex effects still
need explicit, reviewed text renderings or remain unsupported.
"""
import itertools
import re
from decimal import Decimal


def numeric_deltas(effect, field):
    text=re.sub(r'\s+','',effect['effects'])
    atom=rf'at:prestage\{{target:this\{{g\.{re.escape(field)}\+=(-?\d+(?:\.\d+)?)\}}\}}'
    if any(effect[k] not in ('',False) for k in ('actions','conditions','cost','limit','forceInitialHand')):
        raise ValueError('Numeric modifier has additional semantics')
    if effect['max']==1:
        match=re.fullmatch(atom,text)
        if not match:raise ValueError('Unsupported numeric modifier')
        return [Decimal(0),Decimal(match[1])]
    pattern=rf'level:(\d+)\{{({atom})\}}'
    parts=list(re.finditer(pattern,text))
    if ''.join(p[0] for p in parts)!=text or [int(p[1]) for p in parts]!=list(range(1,effect['max']+1)):
        raise ValueError('Incomplete or unsupported level modifiers')
    return [Decimal(0)]+[Decimal(p[3]) for p in parts]


def compile_states(rule, catalog):
    spec=rule['state_template'];card=catalog.skill_cards[rule['card_id']]
    allowed={int(v) for v in card['availableCustomizations'].split(',') if v}
    axes=spec['axes'];ids=[a['effect_id'] for a in axes]
    if len(set(ids))!=len(ids) or set(ids)!=allowed:
        raise ValueError('Every available effect must have exactly one axis')
    choices=[]
    for axis in axes:
        effect=catalog.customizations[axis['effect_id']]
        if axis['kind']=='numeric':
            deltas=numeric_deltas(effect,axis['field'])
            sign=-1 if axis.get('operation')=='decrease' else 1
            values=[str(Decimal(str(axis['base']))+sign*d) for d in deltas]
            variants=[(level,value) for level,value in enumerate(values)]
        elif axis['kind']=='text':
            if len(axis['variants'])!=effect['max']+1:
                raise ValueError('Text axis must explicitly describe every level')
            # null means unsupported, not an absent effect. No state is emitted.
            variants=[(level,value) for level,value in enumerate(axis['variants']) if value is not None]
        else:raise ValueError('Unknown effect axis')
        choices.append(variants)
    cases=[]
    for states in itertools.product(*choices):
        pattern=spec['pattern'];parts=[];case={}
        for axis,(level,value) in zip(axes,states):
            if level:parts.append(f"{axis['effect_id']}x{level}")
            if axis.get('evidence')=='corner-cost':
                case['corner_cost']=int(value)
            else:
                placeholder='{'+axis['name']+'}'
                if placeholder not in pattern:raise ValueError('Unused effect axis')
                pattern=pattern.replace(placeholder,value)
        if re.search(r'\{[a-zA-Z_]\w*\}',pattern):raise ValueError('Unfilled effect placeholder')
        case.update(pattern=pattern,token='e'.join(parts));cases.append(case)
    return cases


def complete_cases(rule,catalog):
    return compile_states(rule,catalog) if rule.get('state_template') else rule.get('complete_text_cases',[])


def is_complete_rule(rule):
    return bool(rule.get('complete_text_cases') or rule.get('state_template'))

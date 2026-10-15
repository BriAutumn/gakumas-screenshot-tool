"""Build a complete, inspectable game-ID / simulator-ID correspondence inventory.

Used by the data updater with public metadata and a staged Catalog. It never
reads owned memories; only its explicit output and parsing cache are written.
"""
from __future__ import annotations
import collections
import hashlib
import json
import unicodedata
from pathlib import Path

from gkshot.catalog import Catalog
from gkshot.resource_paths import RUNTIME_ROOT
from .native_mapping_semantics import native_signature, simulator_signature

CHARACTERS = {'hski': 1, 'ttmr': 2, 'fktn': 3, 'amao': 4, 'kllj': 5,
              'kcna': 6, 'ssmk': 7, 'shro': 8, 'hrnm': 9, 'hume': 10,
              'jsna': 11, 'hmsz': 12, 'atbm': 13}
# Explicit upstream spelling discrepancy, scoped to this native identity.
CARD_ALIASES = {'p_card-02-ido-3_165': '夢と現境界線',
    'p_card-00-sup-2_028': 'ストレッチ談義',
    'p_card-00-sup-3_155': '月明りに包まれて',
    'p_card-02-ido-3_086': '愛をこめて'}
MISSING_PLUS = {'p_card-01-ido-1_058', 'p_card-02-ido-1_057'}

def norm(text):
    return unicodedata.normalize('NFKC', text).replace('\\n', '').replace('\n', '').strip()

def read(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))

def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

def build(source, cache, output, catalog_root=None, sim_data=None):
    catalog = Catalog(catalog_root) if catalog_root else Catalog()
    hashes = {}
    def table(name):
        raw = (source / (name + '.yaml')).read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        hashes[name] = digest
        cached = cache / (name + '-' + digest + '.json')
        if not cached.exists():
            import yaml
            write(cached, yaml.load(raw.decode('utf-8'), Loader=getattr(yaml, "CSafeLoader", yaml.SafeLoader)))
        return read(cached)

    cards, items, idols, customs = [table(n) for n in
        ('ProduceCard', 'ProduceItem', 'IdolCard', 'ProduceCardCustomize')]
    grows = {r['id']: r for r in table('ProduceCardGrowEffect')}
    exams = {r['id']: r for r in table('ProduceExamEffect')}
    labels = {r['type']: r for r in table('ProduceDescriptionProduceCardGrowEffect')}
    triggers = {r['id']: r for r in table('ProduceExamTrigger')}
    sim_data = sim_data or RUNTIME_ROOT / 'upstream/packages/gakumas-data/json'
    sim_idols = read(sim_data / 'p_idols.json')
    character_ids = dict(CHARACTERS)
    if (source / 'Character.yaml').is_file():
        sim_characters = collections.defaultdict(list)
        for row in read(sim_data / 'idols.json'):
            sim_characters[norm(row['name']).replace(' ', '')].append(row['id'])
        for row in table('Character'):
            full_name = norm(row['lastName'] + row['firstName']).replace(' ', '')
            matches = sim_characters[full_name]
            if len(matches) == 1:
                character_ids[row['id']] = matches[0]
    mapping = {k: {} for k in ('cards', 'items', 'idols', 'customizations')}
    evidence = {k: {} for k in mapping}
    inventory = {k: [] for k in mapping}
    names = collections.defaultdict(list)
    for r in catalog.skill_cards.values():
        names[norm(r['name'])].append(r)
    pairs = []
    for card in cards:
        name = card['name']
        alias = CARD_ALIASES.get(card['id'])
        lookup = (alias + '+' * card['upgradeCount']) if alias else name
        if card['id'] in MISSING_PLUS and card['upgradeCount'] == 1:
            lookup = name.removesuffix('+')
        matches = [s for s in names[norm(lookup)]
                   if s['upgraded'] == (card['upgradeCount'] > 0)]
        sid = matches[0]['id'] if len(matches) == 1 else None
        key = card['id'] + ':' + str(card['upgradeCount'])
        if sid is not None:
            mapping['cards'].setdefault(card['id'], {})[str(card['upgradeCount'])] = sid
            pairs.append((card, matches[0]))
        basis = 'reviewed-spelling-alias' if alias or card['id'] in MISSING_PLUS else 'normalized-name-and-upgrade'
        evidence['cards'][key] = {'basis': basis, 'sim_id': sid}
        inventory['cards'].append({'native_id': card['id'], 'upgrade': card['upgradeCount'],
            'native_name': name, 'sim_id': sid,
            'sim_name': matches[0]['name'] if sid else None,
            'status': 'identity-matched' if sid else
                      'unsupported-upgrade-level' if card['upgradeCount'] > 1 else 'identity-unresolved',
            'basis': basis, 'native_options': card['produceCardCustomizeIds'],
            'sim_options': matches[0]['availableCustomizations'] if sid else None,
            'native_hidden': card['libraryHidden']})

    item_names = collections.defaultdict(list)
    for r in catalog.p_items.values():
        if r['mode'] == 'stage':
            item_names[norm(r['name'])].append(r)
    for item in items:
        ms = [s for s in item_names[norm(item['name'])]
              if s['upgraded'] == (item['id'].split('-')[-1] == '1')]
        sid = ms[0]['id'] if len(ms) == 1 else None
        if sid:
            mapping['items'][item['id']] = sid
        inventory['items'].append({'native_id': item['id'], 'native_name': item['name'],
            'sim_id': sid, 'status': 'identity-matched' if sid else 'outside-stage-or-unresolved'})

    for idol in idols:
        ms = [s for s in sim_idols if s['idolId'] == character_ids.get(idol['characterId'])
              and norm(s['title']) == norm(idol['name'])]
        sid = ms[0]['id'] if len(ms) == 1 else None
        if sid:
            mapping['idols'][idol['id']] = sid
        inventory['idols'].append({'native_id': idol['id'], 'native_name': idol['name'],
            'character': idol['characterId'], 'sim_id': sid,
            'status': 'identity-matched' if sid else 'identity-unresolved'})

    groups = collections.defaultdict(list)
    for row in customs:
        groups[row['id']].append(row)
    signatures = collections.defaultdict(list)
    for row in catalog.customizations.values():
        signature = simulator_signature(row)
        if signature:
            signatures[signature].append(row['id'])

    def resolve_exam(eid, trail=()):
        if eid not in exams or eid in trail or len(trail) >= 8:
            return None
        exam = {k: v for k, v in exams[eid].items()
                if k not in ('id', 'produceDescriptions', 'customizeProduceDescriptions')}
        if exam.get('produceCardGrowEffectIds'):
            exam['resolvedGrowEffects'] = [grows[g] for g in exam['produceCardGrowEffectIds']]
        if exam.get('chainProduceExamEffectId'):
            exam['resolvedChainEffect'] = resolve_exam(exam['chainProduceExamEffectId'], (*trail, eid))
        return exam

    for cid, rows in groups.items():
        rows.sort(key=lambda r: r['customizeCount'])
        levels = []
        for row in rows:
            effects = []
            texts = []
            for gid in row['produceCardGrowEffectIds']:
                grow = dict(grows[gid])
                grow.pop('id')
                tid = grow.get('playEffectProduceExamTriggerId') or grow.get('playProduceExamTriggerId')
                if tid in triggers:
                    grow['resolvedTrigger'] = triggers[tid]
                    grow['resolvedTargetTriggers'] = [triggers[t] for t in grow['targetPlayEffectProduceExamTriggerIds']]
                eid = grow['playProduceExamEffectId']
                if eid in exams:
                    grow['examEffect'] = resolve_exam(eid)
                effects.append(grow)
                texts.append(labels.get(grow['effectType'], {}).get('produceCardCustomizeDescription', grow['effectType']))
            levels.append({'level': row['customizeCount'], 'description': row['description'],
                'display_text': row['description'] or ' / '.join(texts), 'effects': effects,
                'native_price': row['producePoint']})
        group = {'native_id': cid, 'levels': levels}
        signature = native_signature(group)
        semantic = signatures.get(signature, []) if signature else []
        uses = [(n, s) for n, s in pairs if cid in n['produceCardCustomizeIds']]
        inferred = set(catalog.customizations)
        for n, s in uses:
            inferred &= {int(x) for x in s['availableCustomizations'].split(',') if x}
        inferred = {i for i in inferred if catalog.customizations[i]['max'] == len(rows)}
        display_names = {norm(part) for l in levels for part in l['display_text'].split(' / ') if part}
        named = {i for i in inferred if norm(catalog.customizations[i]['name']) in display_names}
        if named:
            inferred = named
        sid = semantic[0] if len(semantic) == 1 else None
        basis = 'effect-signature' if sid is not None else 'unresolved'
        if sid is None and signature is None and uses and len(inferred) == 1:
            sid = next(iter(inferred))
            basis = 'card-options-inferred-needs-effect-review'
        if sid is not None:
            mapping['customizations'][cid] = sid
        record = {'native_id': cid, 'sim_id': sid, 'basis': basis,
            'status': 'effect-matched' if basis == 'effect-signature' else
                      'candidate' if sid is not None else 'unresolved',
            'sim_name': catalog.customizations[sid]['name'] if sid else None,
            'semantic_signature': signature, 'semantic_candidates': semantic,
            'option_candidates': sorted(inferred), 'levels': levels,
            'sim_effect': catalog.customizations.get(sid),
            'option_conflicts': [s['id'] for n, s in uses
                if sid is not None and str(sid) not in s['availableCustomizations'].split(',')]}
        inventory['customizations'].append(record)
        evidence['customizations'][cid] = {'basis': basis, 'sim_id': sid}

    provenance = {'catalog_revision': catalog.manifest['revision'],
        'source': 'https://github.com/vertesan/gakumasu-diff', 'source_sha256': hashes,
        'method': 'complete catalog inventory; identity names; exact effect signatures; option candidates marked separately'}
    mapping['provenance'] = provenance
    mapping['evidence'] = evidence
    reverse = {}
    for kind, sim_rows in [('cards', catalog.skill_cards), ('items', catalog.p_items),
                           ('idols', {r['id']: r for r in sim_idols}),
                           ('customizations', catalog.customizations)]:
        assigned = {r['sim_id'] for r in inventory[kind] if r['sim_id'] is not None}
        reverse[kind] = [r for sid, r in sim_rows.items() if sid not in assigned]
    summary = {kind: dict(collections.Counter(r['status'] for r in rs)) for kind, rs in inventory.items()}
    summary['simulator_unassigned'] = {k: len(v) for k, v in reverse.items()}
    write(output / 'mapping.json', mapping)
    write(output / 'full-correspondence.json', {'provenance': provenance,
        'summary': summary, 'inventory': inventory, 'simulator_unassigned': reverse})
    write(output / 'mapping-summary.json', summary)
    for kind, records in inventory.items():
        lines = ['native_id\tupgrade\tnative_name_or_description\tsim_id\tstatus\tbasis']
        for r in records:
            text = r.get('native_name') or ' / '.join(l['display_text'] for l in r.get('levels', []))
            values = [r['native_id'], r.get('upgrade', ''), norm(text),
                      r['sim_id'] if r['sim_id'] is not None else '', r['status'], r.get('basis', '')]
            lines.append('\t'.join(str(v).replace('\t', ' ') for v in values))
        (output / (kind + '-correspondence.tsv')).write_text('\n'.join(lines) + '\n', encoding='utf-8-sig')
    print(json.dumps(summary, ensure_ascii=False))


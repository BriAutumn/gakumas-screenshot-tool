import readline from 'node:readline';
import {readFileSync} from 'node:fs';
import {createHash} from 'node:crypto';
import {format} from 'node:util';
import {IdolConfig, StageConfig, IdolStageConfig, StageEngine, StagePlayer, STRATEGIES, resetRand, getRandCallCount} from 'gakumas-engine';
import {Stages, SkillCards, PItems, PIdols, Customizations} from 'gakumas-data';

const manifest = JSON.parse(readFileSync(new URL('./manifest.json', import.meta.url)));
for (const [path, hash] of Object.entries(manifest.files)) {
  if (createHash('sha256').update(readFileSync(new URL(`./upstream/${path}`, import.meta.url))).digest('hex') !== hash)
    throw new Error(`Upstream integrity mismatch: ${path}`);
}
for (const method of ['log', 'info', 'debug', 'warn'])
  console[method] = (...args) => process.stderr.write(format(...args) + '\n');
const send = value => process.stdout.write(JSON.stringify(value) + '\n');
function check(condition, message) { if (!condition) throw new Error(message); }
const integer = (x, min = 0) => Number.isSafeInteger(x) && x >= min;
const slots = (x, n) => Array.isArray(x) && x.length === n;

function makeConfig(l) {
  check(l?.schema_version === 1, 'Unsupported loadout schema');
  check(integer(l.stage_id, 1), 'Invalid stage ID');
  const stage = Stages.getById(l.stage_id);
  check(stage?.type === 'contest', 'Only ordinary contest stages are supported');
  check(typeof l.support_bonus === 'number' && Number.isFinite(l.support_bonus) && l.support_bonus >= 0, 'Invalid support bonus');
  const params = ['vocal', 'dance', 'visual', 'stamina'].map(k => l.parameters?.[k]);
  check(params.every(x => integer(x)) && params[3] > 0, 'Parameters must be known integers; stamina must be positive');
  check(slots(l.item_ids, 4), 'Expected four item slots');
  for (const id of l.item_ids) check(integer(id) && (!id || PItems.getById(id)?.mode === 'stage'), `Invalid stage item: ${id}`);
  check(slots(l.card_ids, 2) && slots(l.customizations, 2), 'Expected two card groups');
  const groups = l.card_ids.map((row, i) => {
    check(slots(row, 6) && slots(l.customizations[i], 6), 'Expected six slots per card group');
    return row.map((id, j) => {
      check(integer(id) && (!id || SkillCards.getById(id)), `Unknown card: ${id}`);
      const token = l.customizations[i][j];
      check(typeof token === 'string', 'Unknown customization is not empty customization');
      if (token === '') return {};
      check(id && /^\d+x[1-9]\d*(?:e\d+x[1-9]\d*)*$/.test(token), 'Invalid customization token');
      const result = {};
      const allowed = String(SkillCards.getById(id).availableCustomizations).split(',');
      for (const part of token.split('e')) {
        const [effect, level] = part.split('x').map(Number);
        check(!Object.hasOwn(result, effect) && allowed.includes(String(effect)) &&
              integer(level, 1) && level <= Customizations.getById(effect)?.max, `Unsupported customization ${part} on card ${id}`);
        result[effect] = level;
      }
      return result;
    });
  });
  const idol = new IdolConfig({params, supportBonus: l.support_bonus, pItemIds: l.item_ids,
    skillCardIdGroups: l.card_ids, customizationGroups: groups});
  check(idol.plan, 'Cannot infer idol plan from empty/free-only cards and items');
  check(stage.plan === 'free' || stage.plan === idol.plan, 'Idol plan does not match stage');
  for (const [ids, table] of [[l.card_ids.flat(), SkillCards], [l.item_ids, PItems]]) {
    for (const id of ids.filter(Boolean)) {
      const entity = table.getById(id);
      check(entity.plan === 'free' || entity.plan === idol.plan, `Mixed plans: ${id}`);
      // Upstream getIndications: cards require the same character, items the same version.
      check(!entity.pIdolId || (table === SkillCards
        ? PIdols.getById(entity.pIdolId)?.idolId === idol.idolId
        : entity.pIdolId === idol.pIdolId), `Conflicting signature entity: ${id}`);
    }
  }
  return new IdolStageConfig(idol, new StageConfig(stage));
}

const lines = readline.createInterface({input: process.stdin, crlfDelay: Infinity});
for await (const line of lines) {
  let r;
  try {
    r = JSON.parse(line);
    check(r.protocol_version === 1 && typeof r.request_id === 'string', 'Invalid protocol/request ID');
    check(integer(r.trials, 1) && r.trials <= 100000, 'trials must be 1..100000');
    check(integer(r.seed) && r.seed <= 0xffffffff, 'seed must be uint32');
    const config = makeConfig(r.loadout);
    resetRand(r.seed);
    const engine = new StageEngine(config);
    const strategy = new STRATEGIES.HeuristicStrategy(engine);
    engine.strategy = strategy;
    const scores = [];
    for (let i = 0; i < r.trials; i++) {
      const result = await new StagePlayer(engine, strategy).play();
      check(Number.isFinite(result.score), 'Engine returned non-finite score');
      scores.push(result.score);
      if ((i + 1) % Math.max(1, Math.floor(r.trials / 100)) === 0 || i + 1 === r.trials)
        send({protocol_version: 1, request_id: r.request_id, type: 'progress', completed: i + 1, trials: r.trials});
    }
    send({protocol_version: 1, request_id: r.request_id, type: 'result', scores,
      trials: r.trials, seed: r.seed, rng: 'upstream-mulberry32-continuous-v1', rng_calls: getRandCallCount(),
      strategy: 'HeuristicStrategy', engine_revision: manifest.revision, data_revision: manifest.revision,
      node_version: process.version, effective: {idol: config.idol, typeMultipliers: config.typeMultipliers,
        defaultCardIds: config.defaultCardIds, stageId: r.loadout.stage_id}});
  } catch (error) {
    send({protocol_version: 1, request_id: r?.request_id ?? null, type: 'error', error: {code: 'SIMULATION_ERROR', message: error.message}});
  }
}

// Independent same-version reference: original URL parser and engine entry.
// No imports from worker.mjs or Python input mapper.
import {readFileSync} from 'node:fs';
import {loadoutFromQuery, silenceConsole} from './upstream/tests/lib.mjs';
import {IdolConfig, StageConfig, IdolStageConfig, StageEngine, StagePlayer, STRATEGIES, resetRand} from 'gakumas-engine';
import {Stages} from 'gakumas-data';
const request = JSON.parse(readFileSync(0, 'utf8'));
const restore = silenceConsole();
const loadout = loadoutFromQuery(request.query);
resetRand(request.seed);
const idol = new IdolConfig(loadout);
const config = new IdolStageConfig(idol, new StageConfig(Stages.getById(loadout.stageId)));
const engine = new StageEngine(config);
const strategy = new STRATEGIES.HeuristicStrategy(engine);
engine.strategy = strategy;
const scores = [];
for (let i = 0; i < request.trials; i++) scores.push((await new StagePlayer(engine, strategy).play()).score);
restore();
console.log(JSON.stringify({scores, idol, typeMultipliers: config.typeMultipliers, defaultCardIds: config.defaultCardIds}));

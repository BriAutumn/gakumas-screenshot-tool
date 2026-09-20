// Build-time only: use the pinned upstream parser, never its catch-and-return-empty wrapper.
import fs from 'node:fs';
import crypto from 'node:crypto';
import {parseEffects,parsePatches} from './upstream/parser/index.js';
import {transformEffects,transformPatches} from './upstream/transformer/index.js';
const fields=['conditions','cost','actions','effects'];
const manifestPath='assets/recognition/manifest.json';
const manifest=JSON.parse(fs.readFileSync(manifestPath));
const payload={schema:1,source_sha256:{},cards:{},customizations:{}};
for (const [name,key,parse,transform] of [['skill_cards','cards',parseEffects,transformEffects],['customizations','customizations',parsePatches,transformPatches]]) {
  const bytes=fs.readFileSync(`assets/recognition/${name}.json`);
  payload.source_sha256[name]=crypto.createHash('sha256').update(bytes).digest('hex');
  if(payload.source_sha256[name]!==manifest.files[name+'.json'].sha256) throw Error('Input catalog hash mismatch');
  for (const row of JSON.parse(bytes)) {
    payload[key][row.id]=Object.fromEntries(fields.map(f=>[f,transform(parse(row[f]))]));
  }
}
fs.writeFileSync('assets/recognition/catalog-effects.json',JSON.stringify(payload,null,2)+'\n');
manifest.files['catalog-effects.json']={generated_by:'tools/catalog/export.mjs',sha256:crypto.createHash('sha256').update(fs.readFileSync('assets/recognition/catalog-effects.json')).digest('hex')};
fs.writeFileSync(manifestPath,JSON.stringify(manifest,null,2)+'\n');
console.log('Parsed',Object.keys(payload.cards).length,'cards and',Object.keys(payload.customizations).length,'customizations');

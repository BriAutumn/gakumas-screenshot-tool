# Database-derived customization candidates

These tools run locally. Node is required only to rebuild the parsed asset;
normal recognition still uses Python and the existing offline OCR models.

1. `node tools/catalog/export.mjs` parses the pinned catalogs using the vendored
   GKTools parser/transformer. Parsing errors abort; they are never converted to
   empty effects. Input and output hashes are recorded in the asset manifest.
2. `python tools/catalog/audit.py <processing-run> <output-directory>` enumerates
   all customization levels, applies append/anchor patches, and attempts complete
   text generation. This is a read-only text probe, not a memory import.
3. Inspect original images and test generated rules with `CardDetailScanner`.
   Only after image verification, `python tools/catalog/promote.py <card-ids>`
   installs the generated rules. It refuses to overwrite existing rules.

The Python renderer has no card-ID-specific cases. It maps supported semantic
operations to game text. Any unsupported candidate rejects that card's whole
generated rule; it does not discard the difficult state. Ambiguous complete
states are rejected. Changes to hidden cost require bound corner evidence.

The 71 renderable cards in the first audit are a **syntactic coverage count**,
not 71 image-validated cards. Version 0.5.31 promoted 99, 167 and 297;
0.5.32 adds image-validated rules for 97, 131, 379 and 521.
Existing rules stay unchanged. The full-text, green-coverage, source binding,
confidence and independent-resolution checks all remain in the normal scanner
and library. Unknown remains `null`, never an empty customization string.

Upstream EntityDetails displays pre-rendered images; its data parser does not
provide a ready-made Japanese tooltip renderer. This project therefore retains
a small shared text renderer. The next work is to extend this renderer and fix
shared OCR failures, rather than manually write every card's effect sentence.

Vendored parser sources, license and exact hashes are under `upstream/`.
Only explicit `.js` import extensions differ from upstream. Candidate anchor
merge semantics follow GKTools CardManager.getLines / mergePatch. This is not
a stage simulation engine.

// Autosave core check: lifts the pure block out of project.html and exercises it under
// node. Run with `node tests/autosave_check.js`; no npm dependencies, exits non-zero on failure.
'use strict';
const fs = require('fs');
const path = require('path');
const assert = require('assert');

const html = fs.readFileSync(path.join(__dirname, '..', 'project.html'), 'utf8');
const m = html.match(/\/\* ==AUTOSAVE-CORE-BEGIN== \*\/([\s\S]*?)\/\* ==AUTOSAVE-CORE-END== \*\//);
if (!m) { console.error('AUTOSAVE-CORE markers not found in project.html'); process.exit(1); }
const core = new Function(m[1] +
  '\nreturn {autosaveState, asMark, asSnapshot, asBody, asAck, asHasPending, asStale, asConflict, asResolveConflict};')();

let checks = 0;
function ok(name) { checks++; console.log('ok - ' + name); }

// --- generations: every edit re-tags the field, last value wins ---
{
  const st = core.autosaveState('film');
  core.asMark(st, null, 'title', 'one');
  assert.strictEqual(st.pendingTop.title.value, 'one');
  assert.strictEqual(st.pendingTop.title.gen, 1);
  core.asMark(st, null, 'title', 'two');
  assert.strictEqual(st.pendingTop.title.value, 'two');
  assert.strictEqual(st.pendingTop.title.gen, 2);
  core.asMark(st, 's1', 'action', 'run');
  core.asMark(st, 's2', 'action', 'hide');
  assert.strictEqual(st.pendingScene.s1.action.gen, 1);
  ok('asMark bumps the generation per field and keeps the latest value');
}

// --- request body: dirty-only delta for field edits, full list for structure, no gen leak ---
{
  const st = core.autosaveState('film');
  st.rev = 7;
  core.asMark(st, null, 'title', 'two');
  core.asMark(st, 's1', 'seconds', 9);
  const scenes = [{id: 's1'}, {id: 's2'}];
  const body = core.asBody(core.asSnapshot(st), scenes);
  assert.strictEqual(body.title, 'two');
  assert.strictEqual(body.base_rev, 7);
  assert.deepStrictEqual(body.scenes, [{id: 's1', seconds: 9}], 'clean scenes are not sent at all');
  assert.strictEqual(body.scenes_delta, true, 'a field-only save is a delta patch');
  assert.strictEqual(JSON.stringify(body).includes('gen'), false);
  ok('asBody sends a dirty-scenes-only delta with values, base_rev and no gen leak');

  // structure pending: the complete list goes out, unflagged, carrying every scene
  st.structure = [{id: 's2'}, {id: 's1'}];
  const full = core.asBody(core.asSnapshot(st), st.structure);
  assert.deepStrictEqual(full.scenes, [{id: 's2'}, {id: 's1', seconds: 9}]);
  assert.strictEqual(full.scenes_delta, undefined, 'a structure save is the whole list');
  ok('asBody sends the full scene list when the structure changed');
}

// --- ack: gen-matched clears, a newer edit made during the request survives ---
{
  const st = core.autosaveState('film');
  core.asMark(st, null, 'title', 'sent');
  core.asMark(st, 's1', 'action', 'sent action');
  core.asMark(st, 's2', 'title', 'other scene');
  const snap = core.asSnapshot(st);
  core.asMark(st, null, 'title', 'typed during the request');   // edits made while the save was out
  core.asMark(st, 's1', 'prompt', 'typed during the request');
  core.asMark(st, 's2', 'title', 'retyped during the request');
  core.asAck(st, snap);
  assert.deepStrictEqual(st.pendingTop.title, {value: 'typed during the request', gen: 2},
    'the newer title draft must stay pending');
  assert.strictEqual(st.pendingScene.s1.action, undefined, 'gen-matched action cleared');
  assert.strictEqual(st.pendingScene.s1.prompt.value, 'typed during the request', 'typing after the snapshot survives');
  assert.strictEqual(st.pendingScene.s2.title.value, 'retyped during the request', 'a field retyped mid-save survives its ack');
  ok('asAck clears only generation-matched fields');

  // acking the newer generation afterwards empties the scene entry
  const snap2 = core.asSnapshot(st);
  core.asAck(st, snap2);
  assert.deepStrictEqual(st.pendingTop, {});
  assert.deepStrictEqual(st.pendingScene, {}, 'emptied scene entries are deleted');

  // a scene deleted while the save was out: ack skips it
  const st2 = core.autosaveState('film');
  core.asMark(st2, 'gone', 'title', 'x');
  const snapGone = core.asSnapshot(st2);
  delete st2.pendingScene.gone;
  core.asAck(st2, snapGone);
  assert.deepStrictEqual(st2.pendingScene, {});
  ok('asAck survives a scene deleted while the save was in flight');
}

// --- structure: cleared only when it is still the very list that was sent ---
{
  const st = core.autosaveState('film');
  const listA = [{id: 'a'}, {id: 'b'}];
  st.structure = listA;
  const snap = core.asSnapshot(st);
  core.asAck(st, snap);
  assert.strictEqual(st.structure, null, 'acked structure clears');
  st.structure = listA;
  const snap2 = core.asSnapshot(st);
  const listB = [{id: 'b'}, {id: 'a'}];
  st.structure = listB;                       // user clicked again while the save was out
  core.asAck(st, snap2);
  assert.strictEqual(st.structure, listB, 'a newer structure survives the older ack');
  ok('structure clears on ack only when unchanged since the request');
}

// --- asHasPending ---
{
  const st = core.autosaveState('film');
  assert.strictEqual(core.asHasPending(st), false);
  core.asMark(st, 's9', 'title', 'x');
  assert.strictEqual(core.asHasPending(st), true);
  core.asAck(st, core.asSnapshot(st));
  assert.strictEqual(core.asHasPending(st), false);
  st.structure = [{id: 'x'}];
  assert.strictEqual(core.asHasPending(st), true);
  ok('asHasPending reports field drafts and pending structure');
}

// --- stale responses after a project switch ---
{
  const st = core.autosaveState('film-a');
  assert.strictEqual(core.asStale(st, 'film-a'), false);
  assert.strictEqual(core.asStale(st, 'film-b'), true);
  st.slug = 'film-b';                         // what open() does
  assert.strictEqual(core.asStale(st, 'film-a'), true, 'an in-flight response for the old film is dropped');
  ok('asStale drops responses aimed at a project we left');
}

// --- conflict: 409 keeps the draft; Keep mine vs Reload ---
{
  const st = core.autosaveState('film');
  st.rev = 3;
  core.asMark(st, null, 'title', 'mine');
  core.asMark(st, 's1', 'action', 'also mine');
  core.asConflict(st, ['prompt'], 9);
  assert.deepStrictEqual(st.conflict, {fields: ['prompt'], rev: 9});
  assert.strictEqual(st.pendingTop.title.value, 'mine', 'the 409 must not touch the draft');

  const rev = core.asResolveConflict(st, true);         // Keep mine
  assert.strictEqual(rev, 9);
  assert.strictEqual(st.conflict, null);
  assert.strictEqual(st.pendingTop.title.value, 'mine', 'Keep mine keeps every draft');
  assert.strictEqual(st.rev, 3, 'rev only moves when the caller adopts it');
  ok('a 409 leaves the draft pending; Keep mine returns the server rev to resend against');

  core.asConflict(st, ['title'], 11);
  assert.strictEqual(core.asResolveConflict(st, false), null);  // Reload
  assert.strictEqual(st.rev, 11, 'Reload adopts the server revision');
  assert.strictEqual(core.asHasPending(st), false, 'Reload discards the draft');
  ok('Reload discards the draft and adopts the server revision');
}

console.log('all ' + checks + ' autosave core checks passed');

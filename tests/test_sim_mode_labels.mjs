import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source = fs.readFileSync(new URL('../pi/deck-ui/js/sim.js', import.meta.url), 'utf8');
const start = source.indexOf('var AP_MODE_LABEL =');
const end = source.indexOf('\nfunction simSimple', start);
assert(start >= 0 && end > start);
const nodes = new Map();
const context = vm.createContext({
  AP_TILES: { aphdg: 'ap_hdg' },
  $: id => {
    if (!nodes.has(id)) nodes.set(id, {});
    return nodes.get(id);
  },
});
vm.runInContext(source.slice(start, end), context);
context.simPaintModes({
  ap_master: { state: 'off' },
  ap_hdg: { mode: 'on', source: 'ini-a330', mode_label: 'HDG SELECTED' },
});
assert.equal(nodes.get('simm-aphdg').textContent, 'HDG SELECTED · ON');
assert.equal(nodes.get('simn-aphdg').textContent, 'HDG SELECTED · AP OFF');
assert(!nodes.get('simn-aphdg').className.includes('live'));
context.simPaintModes({
  ap_master: { state: 'engaged' },
  ap_hdg: { mode: 'on', source: 'ini-a330', mode_label: 'HDG SELECTED' },
});
assert.equal(nodes.get('simn-aphdg').textContent, 'AP FLYING SELECTED HEADING');
context.simPaintModes({ ap_master: { state: 'off' }, ap_hdg: { mode: 'off' } });
assert.equal(nodes.get('simm-aphdg').textContent, 'HDG HOLD · OFF');
console.log('A330 heading status labels passed; generic labels unchanged');

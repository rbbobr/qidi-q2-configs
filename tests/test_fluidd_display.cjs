const fs = require('fs');
const vm = require('vm');
const assert = require('assert/strict');
const source = fs.readFileSync(__dirname + '/../tools/fluidd_material_display.js', 'utf8');
const adapt = vm.runInNewContext(source + '\nq2DisplayFilamentItems');
const slots = Array(17).fill(0); slots[1] = 1;
const items = Array.from({length: 18}, (_, index) => Object.freeze({
  label: String(index), filament: '', color: '#00000000', vendor: '', isRotating: false
}));
Object.freeze(items);
const records = [{index: 1, filament_str: 'PA6-CF', vendor_str: 'QIDI', color_rgb_str: ['#060606']}];
const printer = {box_extras: {boxes: [{filament_info: records}]}};
const original = JSON.stringify(printer);
let result = adapt(items, printer, slots);
assert.equal(result[1].filament, 'PA6-CF');
assert.equal(result[1].color, '#060606');
assert.equal(result[1].vendor, 'QIDI');
assert.equal(result[0].filament, '');
assert.equal(result[16].filament, '');
assert.equal(items[1].filament, '');
assert.equal(JSON.stringify(printer), original);
assert.equal(JSON.stringify(adapt(items, {}, slots)), JSON.stringify(items));
assert.equal(JSON.stringify(adapt(items, printer, Array(17).fill(0))), JSON.stringify(items));
records[0].color_rgb_str = ['invalid'];
result = adapt(items, printer, slots);
assert.equal(result[1].color, '#00000000');
records[0].color_rgb_str = 'abcdef';
assert.equal(adapt(items, printer, slots)[1].color, '#abcdef');
console.log('Display adapter: live metadata, empty/missing data, color validation, unchanged inputs passed.');

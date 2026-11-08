// Read-only display adapter for QIDI's newer filament metadata.
// No requests, commands, store writes, slot mappings, or control changes.
function q2DisplayFilamentItems(items, printer, slots) {
  const result = items.map(item => item && ({ ...item }));
  const boxes = printer?.box_extras?.boxes;
  if (!Array.isArray(boxes)) return result;
  for (let index = 0; index < 16; index++) {
    if (slots[index] !== 1 || !result[index]) continue;
    const records = boxes[Math.floor(index / 4)]?.filament_info;
    if (!Array.isArray(records)) continue;
    const info = records.find(record => record?.index === index % 4);
    if (!info) continue;
    const item = result[index];
    const name = info.filament_str || info.filament_type;
    if (typeof name === 'string' && name.trim()) item.filament = name;
    if (typeof info.vendor_str === 'string' && info.vendor_str.trim()) {
      item.vendor = info.vendor_str;
    }
    const rawColor = Array.isArray(info.color_rgb_str)
      ? info.color_rgb_str[0] : info.color_rgb_str;
    if (typeof rawColor === 'string' && /^#?[0-9a-f]{6}$/i.test(rawColor)) {
      item.color = '#' + rawColor.replace(/^#/, '');
    }
  }
  return result;
}

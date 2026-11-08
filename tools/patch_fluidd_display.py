"""Patch only Material Control's displayed slot metadata in the QIDI bundle.

No Klipper configuration, Box settings, mapping or action handler is modified.
Python 3.9+, no dependencies. Defaults to check-only. Creates versioned JS assets
and changes index.html/sw.js; original assets remain available for rollback.
"""
from pathlib import Path
import argparse
import hashlib
import json
import os
import re
import shutil
import time

MARKER = '// Q2_DISPLAY_ONLY_V1'
OLD = 'isRotating:!1},t[17]={label:"",color:"#00000000",filament:"",vendor:"",isRotating:!1},t}get filamentItems()'
NEW = OLD.replace(',t}get filamentItems()', ',q2DisplayFilamentItems(t,this.$store.state.printer.printer,this.variables.slot)}get filamentItems()')

def digest(data):
    return hashlib.sha256(data).hexdigest()

def prepare(root):
    index = (root / 'index.html').read_text(encoding='utf-8')
    found = re.findall(r'<script[^>]+src=["\'](\.?/?assets/index-[^"\']+\.js)["\']', index)
    if len(found) != 1:
        raise RuntimeError('Expected exactly one main bundle in index.html')
    bundle = (root / found[0]).resolve()
    if bundle.parent != (root / 'assets').resolve():
        raise RuntimeError('Unexpected bundle path')
    if '.q2display-' in bundle.name:
        raise RuntimeError('Versioned display adapter is already installed; restore backup before upgrading')
    sw = root / 'sw.js'
    index_path = root / 'index.html'
    before = {index_path: index_path.read_bytes(), sw: sw.read_bytes()}
    source = bundle.read_text(encoding='utf-8')
    if MARKER in source:
        prefix, previous_helper = source.split('\n' + MARKER + '\n', 1)
        if prefix.count(NEW) != 1:
            raise RuntimeError('Installed adapter hook does not match; no changes made')
        source = prefix.replace(NEW, OLD)
    if source.count(OLD) != 1 or 'q2DisplayFilamentItems' in source:
        raise RuntimeError('Unsupported frontend version: expected display getter is not unique')
    helper = Path(__file__).with_name('fluidd_material_display.js').read_text(encoding='utf-8')
    patched = source.replace(OLD, NEW) + '\n' + MARKER + '\n' + helper
    # Version the reverse dependency closure, including circular imports. Updating
    # just the entry URL can otherwise load a second copy through a cached chunk.
    paths = {p.name: p for p in (root / 'assets').glob('*.js')
             if '.q2display-' not in p.name}
    def refers_to(path, names):
        needles = [name.encode() for name in names]
        tail = b''
        overlap = max(map(len, needles))
        with path.open('rb') as stream:
            while True:
                block = stream.read(65536)
                if not block:
                    return False
                data = tail + block
                if any(needle in data for needle in needles):
                    return True
                tail = data[-overlap:]
    changed = {bundle.name}
    while True:
        parents = {name for name, path in paths.items()
                   if name not in changed and refers_to(path, changed)}
        expanded = changed | parents
        if expanded == changed:
            break
        changed = expanded
    sources = {name: patched if name == bundle.name else paths[name].read_text(encoding='utf-8')
               for name in changed}
    version = digest(''.join(name + sources[name] for name in sorted(changed)).encode())[:12]
    names = {name: name[:-3] + '.q2display-' + version + '.js' for name in changed}
    def rewrite(text):
        for old, new in names.items():
            text = text.replace(old, new)
        return text
    after = {}
    for name in sorted(changed):
        target = root / 'assets' / names[name]
        if target.exists():
            raise RuntimeError('Versioned target already exists: ' + str(target))
        before[target] = None
        after[target] = rewrite(sources[name]).encode('utf-8')
    old_sw = before[sw].decode('utf-8')
    patched_sw = rewrite(old_sw)
    patched_index = rewrite(index).encode('utf-8')
    for url, data in [('assets/' + names[name], after[root / 'assets' / names[name]])
                      for name in sorted(changed)] + [('index.html', patched_index)]:
        pattern = r'\{"revision":(?:null|"[^"]*"),"url":"' + re.escape(url) + r'"\}'
        if len(re.findall(pattern, patched_sw)) != 1:
            raise RuntimeError('Expected exactly one service worker entry: ' + url)
        entry = json.dumps({'revision': digest(data), 'url': url}, separators=(',', ':'))
        patched_sw = re.sub(pattern, lambda _: entry, patched_sw)
    # Publish entrypoints last, after all new assets have been verified.
    after[sw] = patched_sw.encode('utf-8')
    after[index_path] = patched_index
    return before, after

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True, type=Path)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    root = args.root.resolve(strict=True)
    before, after = prepare(root)
    report = {str(path.relative_to(root)): {'before': digest(before[path]) if before[path] is not None else None, 'after': digest(data)}
              for path, data in after.items()}
    print(json.dumps(report, indent=2), flush=True)
    if not args.apply:
        print('CHECK ONLY: no files changed')
        return
    backup = root / ('codex-display-backup-' + time.strftime('%Y%m%dT%H%M%SZ', time.gmtime()))
    backup.mkdir(exist_ok=False)
    for path, data in before.items():
        if data is None:
            continue
        target = backup / path.relative_to(root)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        assert target.read_bytes() == data
    (backup / 'manifest.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    try:
        for path, data in after.items():
            if (path.read_bytes() if path.exists() else None) != before[path]:
                raise RuntimeError('Concurrent change: ' + str(path))
            stat = (path if path.exists() else root / 'index.html').stat()
            temp = path.with_name(path.name + '.codex-display-tmp')
            with temp.open('xb') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temp, stat.st_mode)
            if hasattr(os, 'chown'):
                os.chown(temp, stat.st_uid, stat.st_gid)
            os.replace(temp, path)
            assert path.read_bytes() == data
    except Exception:
        for path in before:
            if before[path] is not None:
                shutil.copy2(backup / path.relative_to(root), path)
        raise
    print('Applied display-only adapter. Backup:', backup, flush=True)

if __name__ == '__main__':
    main()

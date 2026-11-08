import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

script = Path(__file__).resolve().parents[1] / 'tools' / 'patch_fluidd_display.py'
spec = importlib.util.spec_from_file_location('patcher', script)
patcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(patcher)


class AssetGraphTest(unittest.TestCase):
    def test_circular_imports_and_service_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            assets = root / 'assets'
            assets.mkdir()
            (root / 'index.html').write_text('<script src="./assets/index-test.js"></script>')
            (assets / 'index-test.js').write_text('import("./camera.js");' + patcher.OLD)
            (assets / 'camera.js').write_text('import {x} from "./index-test.js";')
            (assets / 'unrelated.js').write_text('const value = 1;')
            entries = [{'revision': None, 'url': 'assets/' + name}
                       for name in ['index-test.js', 'camera.js', 'unrelated.js']]
            entries.append({'revision': 'old', 'url': 'index.html'})
            (root / 'sw.js').write_text(json.dumps(entries, separators=(',', ':')))
            before, after = patcher.prepare(root)
            new_assets = {path.name: data.decode() for path, data in after.items() if path.parent == assets}
            self.assertEqual(len(new_assets), 2)
            for source in new_assets.values():
                self.assertNotIn('./index-test.js', source)
                self.assertNotIn('./camera.js', source)
            index_name = next(name for name in new_assets if name.startswith('index-'))
            self.assertIn(index_name, after[root / 'index.html'].decode())
            updated = json.loads(after[root / 'sw.js'])
            for entry in updated:
                path = root / entry['url']
                if path in after:
                    self.assertEqual(entry['revision'], patcher.digest(after[path]))
            self.assertNotIn(assets / 'unrelated.js', after)
            self.assertTrue(all(before[assets / name] is None for name in new_assets))
            self.assertIn(patcher.OLD, (assets / 'index-test.js').read_text())


if __name__ == '__main__':
    unittest.main()

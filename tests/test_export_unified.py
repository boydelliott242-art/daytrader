"""scripts/export_unified.py: every published JSON lands in one bundle; nothing else is written."""
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))
import export_unified  # noqa: E402


class ExportUnifiedTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_bundle_holds_every_published_json_and_skips_bad_files(self):
        pub = os.path.join(self.tmp, "publish")
        os.makedirs(pub)
        with open(os.path.join(pub, "rankings.json"), "w") as fh:
            json.dump({"lists": {"top_momentum_longs": [{"symbol": "MPC"}]}}, fh)
        with open(os.path.join(pub, "regime.json"), "w") as fh:
            json.dump({"tags": ["trend_flat"]}, fh)
        with open(os.path.join(pub, "broken.json"), "w") as fh:
            fh.write("{not json")
        with open(os.path.join(pub, "notes.txt"), "w") as fh:
            fh.write("ignored")
        out = os.path.join(self.tmp, "out", "daytrader.json")
        bundle = export_unified.export(pub, out)
        self.assertEqual(sorted(bundle["files"]), ["rankings.json", "regime.json"])
        self.assertEqual(bundle["skipped"], [{"file": "broken.json", "why": "JSONDecodeError"}])
        with open(out) as fh:
            on_disk = json.load(fh)
        self.assertEqual(on_disk["files"]["rankings.json"]["lists"]["top_momentum_longs"][0]["symbol"], "MPC")
        self.assertEqual(on_disk["source"], "daytrader")
        self.assertEqual(sorted(os.listdir(pub)), ["broken.json", "notes.txt", "rankings.json", "regime.json"])
        self.assertEqual(os.listdir(os.path.dirname(out)), ["daytrader.json"])

    def test_missing_publish_folder_gives_an_empty_bundle(self):
        out = os.path.join(self.tmp, "daytrader.json")
        bundle = export_unified.export(os.path.join(self.tmp, "nope"), out)
        self.assertEqual(bundle["files"], {})
        self.assertTrue(os.path.exists(out))


if __name__ == "__main__":
    unittest.main()

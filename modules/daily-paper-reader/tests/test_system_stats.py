"""system_stats 单元测试：进程树结构、目录体积、GPU 空值兜底、快照组装。"""

import importlib.util
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load_module():
    spec = importlib.util.spec_from_file_location("system_stats_mod", ROOT / "src" / "system_stats.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules["system_stats_mod"] = mod
    spec.loader.exec_module(mod)
    return mod


class ProcessTreeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_module()

    def test_tree_contains_self_with_rss(self):
        tree = self.mod._process_tree()
        self.assertIn("pid", tree["self"])
        self.assertIsInstance(tree["self"]["rss_mb"], float)
        self.assertGreater(tree["self"]["rss_mb"], 0)
        self.assertIsInstance(tree["children"], list)
        entry = tree["self"]
        self.assertIn("name", entry)
        self.assertIn("cmdline", entry)
        self.assertLessEqual(len(entry["cmdline"]), self.mod._CMDLINE_MAX_CHARS)


class DirectorySizeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_module()

    def test_counts_files_and_bytes(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            (root / "a.txt").write_bytes(b"x" * 1000)
            sub = root / "sub"
            sub.mkdir()
            (sub / "b.bin").write_bytes(b"y" * 2500)
            info = self.mod._directory_size(root)
            self.assertEqual(info["files"], 2)
            self.assertGreaterEqual(info["mb"], 0.0)
            self.assertLess(info["mb"], 1.0)

    def test_missing_dir_returns_zeros(self):
        info = self.mod._directory_size(pathlib.Path("Z:/definitely/not/exist"))
        self.assertEqual(info["files"], 0)
        self.assertEqual(info["mb"], 0.0)


class GpuSnapshotTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_module()

    def test_returns_none_or_valid_list(self):
        gpus = self.mod._gpu()
        if gpus is not None:
            self.assertIsInstance(gpus, list)
            for item in gpus:
                self.assertIn("vram_used_mb", item)
                self.assertIn("name", item)


class SnapshotAssemblyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_module()

    def test_snapshot_assembles_all_sections(self):
        snap = self.mod.system_snapshot(
            ROOT,
            tracked_dirs=[ROOT / "tests"],
            job_counts={"survey_jobs": 3, "workflow_runs": 1},
        )
        if not snap.get("ok"):
            self.assertIn("psutil", snap.get("error", ""))  # 无 psutil 环境的降级提示
            return
        self.assertIn("process", snap)
        self.assertIn("gpu", snap)
        self.assertEqual(snap["jobs"]["survey_jobs"], 3)
        disk = snap["disk"][0]
        self.assertEqual(pathlib.Path(disk["path"]).name, "tests")
        self.assertGreaterEqual(disk["files"], 1)


if __name__ == "__main__":
    unittest.main()

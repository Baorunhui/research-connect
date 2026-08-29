import importlib.util
import pathlib
import unittest


def _load_module(module_name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(module_name, path)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


class RrfQueryKeyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = pathlib.Path(__file__).resolve().parents[1]
        src_dir = root / "src"
        cls.mod = _load_module("rrf_mod", src_dir / "2.3.retrieval_papers_rrf.py")

    def test_same_tag_different_query_text_should_not_collide(self):
        q1 = {"type": "llm_query", "paper_tag": "query:SR", "query_text": "symbolic regression + rl"}
        q2 = {"type": "llm_query", "paper_tag": "query:SR", "query_text": "symbolic regression + physics"}
        k1 = self.mod.make_query_key(q1)
        k2 = self.mod.make_query_key(q2)
        self.assertNotEqual(k1, k2)

    def test_key_contains_type_tag_text(self):
        q = {"type": "keyword", "paper_tag": "keyword:SR", "query_text": "Symbolic Regression"}
        self.assertEqual(
            self.mod.make_query_key(q),
            ("keyword", "keyword:SR", "Symbolic Regression"),
        )


if __name__ == "__main__":
    unittest.main()


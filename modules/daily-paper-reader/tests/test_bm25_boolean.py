import importlib.util
import pathlib
import sys
import unittest


class BM25BooleanMixedTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = pathlib.Path(__file__).resolve().parents[1]
        src_dir = root / 'src'
        if str(src_dir) not in sys.path:
            sys.path.insert(0, str(src_dir))

        mod_path = src_dir / '2.1.retrieval_papers_bm25.py'
        spec = importlib.util.spec_from_file_location('bm25_mod', mod_path)
        module = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        spec.loader.exec_module(module)
        cls.mod = module

    def test_boolean_mixed_filters_and_scores(self):
        Paper = self.mod.Paper
        papers = [
            Paper(id='1', title='A B method', abstract='for science', authors=['X']),
            Paper(id='2', title='A C method', abstract='for science', authors=['X']),
            Paper(id='3', title='A D method', abstract='for science', authors=['X']),
        ]
        bm25 = self.mod.build_bm25_index(papers)
        scores = self.mod.score_boolean_mixed_for_query(
            bm25=bm25,
            papers=papers,
            expr='(A AND B) OR (A AND C) AND NOT D',
            or_soft_weight=0.3,
        )
        self.assertGreaterEqual(scores[0], 0)
        self.assertGreaterEqual(scores[1], 0)
        self.assertLess(scores[2], 0)


if __name__ == '__main__':
    unittest.main()

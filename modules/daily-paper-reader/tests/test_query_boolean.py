import unittest

from src.query_boolean import (
    parse_boolean_expr,
    evaluate_expr,
    split_or_branches,
    collect_unique_positive_terms,
    clean_expr_for_embedding,
)


class QueryBooleanTest(unittest.TestCase):
    def test_parse_and_evaluate_precedence(self):
        expr = 'A AND (B OR C) AND NOT D'
        node = parse_boolean_expr(expr)
        self.assertIsNotNone(node)

        self.assertTrue(
            evaluate_expr(node, title='A B paper', abstract='', authors=[])
        )
        self.assertTrue(
            evaluate_expr(node, title='A C paper', abstract='', authors=[])
        )
        self.assertFalse(
            evaluate_expr(node, title='A D paper', abstract='contains B', authors=[])
        )
        self.assertFalse(
            evaluate_expr(node, title='A paper', abstract='nothing relevant', authors=[])
        )

    def test_author_term(self):
        expr = 'author:"Yoshua Bengio" AND diffusion'
        node = parse_boolean_expr(expr)
        self.assertIsNotNone(node)
        self.assertTrue(
            evaluate_expr(
                node,
                title='diffusion model',
                abstract='x',
                authors=['Yoshua Bengio', 'Someone'],
            )
        )
        self.assertFalse(
            evaluate_expr(
                node,
                title='diffusion model',
                abstract='x',
                authors=['Another Author'],
            )
        )

    def test_split_or_branches_and_positive_terms(self):
        expr = '(A AND B) OR (C AND NOT D)'
        node = parse_boolean_expr(expr)
        branches = split_or_branches(node)
        self.assertEqual(len(branches), 2)
        terms0 = collect_unique_positive_terms(branches[0])
        terms1 = collect_unique_positive_terms(branches[1])
        self.assertIn('A', terms0)
        self.assertIn('B', terms0)
        self.assertIn('C', terms1)
        self.assertNotIn('D', terms1)

    def test_clean_expr_for_embedding(self):
        expr = 'Symbolic Regression AND (physics OR astronomy) AND NOT survey'
        cleaned = clean_expr_for_embedding(expr)
        self.assertIn('Symbolic Regression', cleaned)
        self.assertIn('physics', cleaned)
        self.assertIn('astronomy', cleaned)
        self.assertIn('survey', cleaned)
        self.assertNotIn('AND', cleaned.upper())


if __name__ == '__main__':
    unittest.main()

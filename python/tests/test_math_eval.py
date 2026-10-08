"""Boundary coverage for the restricted calculator evaluator."""

from __future__ import annotations

import math
import unittest
from unittest.mock import patch

from wechat_receiver.math_eval import MathEvaluationError, evaluate


class MathEvaluationTests(unittest.TestCase):
    def assert_math_error(self, expression: str, message: str | None = None) -> None:
        with self.assertRaises(MathEvaluationError) as raised:
            evaluate(expression)
        if message is not None:
            self.assertEqual(message, str(raised.exception))

    def test_degree_trigonometry_has_exact_axis_values(self) -> None:
        self.assertEqual(0, evaluate("cos(90)"))
        self.assertEqual(0, evaluate("sin(180)"))
        self.assertEqual(0, evaluate("sin(360)"))
        self.assertEqual(1, evaluate("sin(90)"))
        self.assertEqual(1, evaluate("cos(360)"))
        for expression in ("1 / cos(90)", "1 / sin(0)", "1 / sin(180)"):
            self.assert_math_error(expression, "除数不能为0")

    def test_degree_trigonometry_normalizes_large_and_negative_angles(self) -> None:
        self.assertAlmostEqual(0.5, evaluate("sin(360000000030)"))
        self.assertAlmostEqual(-0.5, evaluate("sin(-30)"))
        self.assertAlmostEqual(0.5, evaluate("cos(-60)"))
        self.assertAlmostEqual(-1, evaluate("sin(-30) / cos(-60)"))

    def test_degree_trigonometry_preserves_small_values_and_near_axis_tangent(self) -> None:
        tiny_negative = evaluate("sin(-0.000000000001)")
        self.assertLess(tiny_negative, 0)
        self.assertNotEqual(0, tiny_negative)
        near_axis_tangent = evaluate("tan(89.999999)")
        self.assertTrue(math.isfinite(near_axis_tangent))
        self.assertGreater(near_axis_tangent, 1_000_000)

    def test_tangent_is_rejected_at_each_right_angle_axis(self) -> None:
        for expression in ("tan(90)", "tan(270)", "tan(-90)", "tan(450)"):
            self.assert_math_error(expression, "tan 在 90° 的奇数倍处无意义")

    def test_bad_function_argument_counts_are_friendly_errors(self) -> None:
        for expression in ("round(1, 2, 3)", "round()", "abs(1, 2)", "sqrt()"):
            self.assert_math_error(expression)

    def test_function_domain_and_overflow_errors_remain_user_facing(self) -> None:
        for expression in ("sqrt(-4)", "log(0)", "ln(-1)"):
            self.assert_math_error(expression, "函数定义域错误")
        self.assert_math_error("exp(1000)", "函数计算溢出")

    def test_expensive_exponents_are_bounded_before_execution(self) -> None:
        for expression in ("9 ** 9 ** 9", "10 ** 1000000"):
            self.assert_math_error(expression, "指数过大")

    def test_too_long_input_is_rejected_before_parsing(self) -> None:
        expression = "(" * 1000 + "1" + ")" * 1000
        with patch("wechat_receiver.math_eval.ast.parse") as parse:
            self.assert_math_error(expression, "表达式不能超过 200 个字符")
        parse.assert_not_called()

    def test_parser_recursion_failure_becomes_a_friendly_expression_error(self) -> None:
        with patch("wechat_receiver.math_eval.ast.parse", side_effect=RecursionError):
            self.assert_math_error("1 + 2", "表达式嵌套过深")


if __name__ == "__main__":
    unittest.main()

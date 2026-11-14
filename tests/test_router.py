import importlib.util
import json
import unittest
from test_config import example
from agy_pool.config import parse_config


class RouterTests(unittest.TestCase):
    def test_plan_is_deterministic_conditional_same_model_first(self):
        self.assertIsNotNone(importlib.util.find_spec('agy_pool.router'), 'planner missing')
        from agy_pool.router import plan_route
        config = parse_config(json.dumps(example()))
        plan = plan_route(config, 'interactive/quality')
        self.assertEqual(plan, plan_route(config, 'interactive/quality'))
        self.assertEqual(plan.mode, 'simulation')
        self.assertEqual(plan.required_capabilities, ('tools',))
        self.assertEqual(plan.max_attempts, 3)
        self.assertEqual([(s.model, s.accounts, s.requires_failure) for s in plan.stages], [
            ('claude-sonnet-4-6', ('primary', 'secondary'), ()),
            ('gemini-3.1-pro', ('primary', 'secondary'), ('unsupported_model',)),
        ])
        self.assertIn('identity_verified', plan.prerequisites)
        self.assertIn('required_capabilities_verified_per_account_model', plan.prerequisites)
        self.assertIn('pre_output_only', plan.prerequisites)
        self.assertEqual(plan_route(config, 'background/fast').stages[0].accounts, ('secondary',))

    def test_empty_fallback_policy_never_permits_another_model(self):
        from agy_pool.router import plan_route
        data = example()
        data['routes']['interactive/quality']['fallback_on'] = []
        plan = plan_route(parse_config(json.dumps(data)), 'interactive/quality')
        self.assertEqual(len(plan.stages), 1)

    def test_disabled_accounts_and_empty_pool_never_cross_group(self):
        from agy_pool.router import plan_route
        data = example()
        data['accounts']['secondary']['enabled'] = False
        config = parse_config(json.dumps(data))
        self.assertEqual(plan_route(config, 'interactive/quality').stages[0].accounts, ('primary',))
        self.assertEqual(plan_route(config, 'background/fast').stages, ())

    def test_unknown_route_is_explicit_local_error(self):
        from agy_pool import router
        self.assertTrue(hasattr(router, 'PlanError'), 'typed planning error missing')
        with self.assertRaises(router.PlanError):
            router.plan_route(parse_config(json.dumps(example())), 'missing')


if __name__ == '__main__':
    unittest.main()

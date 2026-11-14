"""Run from a checkout: python -m agy_pool --help."""
import argparse
from dataclasses import asdict
import json
import sys
from .config import ConfigError, load_config
from .router import PlanError, plan_route


def main() -> int:
    parser = argparse.ArgumentParser(description='Offline pool validation and routing simulation only')
    commands = parser.add_subparsers(dest='command', required=True)
    validate = commands.add_parser('validate', help='Validate strict JSON; no identity or live checks')
    validate.add_argument('config')
    plan = commands.add_parser('plan', help='Print a conditional simulation; never execute requests')
    plan.add_argument('config')
    plan.add_argument('route')
    plan.add_argument('--dry-run', action='store_true', required=True,
                      help='Required acknowledgement: simulation only')
    args = parser.parse_args()
    try:
        config = load_config(args.config)
        output = ({'valid': True, 'mode': 'offline-validation'} if args.command == 'validate'
                  else asdict(plan_route(config, args.route)))
    except (ConfigError, PlanError) as exc:
        print(f'error: {exc}', file=sys.stderr)
        return 2
    except (OSError, UnicodeError):
        print('error: Cannot read UTF-8 configuration', file=sys.stderr)
        return 2
    print(json.dumps(output, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

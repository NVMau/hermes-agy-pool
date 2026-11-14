"""Pure conditional plans, not executable attempts or eligibility claims."""
from dataclasses import dataclass
from .config import Config


@dataclass(frozen=True)
class Stage:
    model: str
    accounts: tuple[str, ...]
    requires_failure: tuple[str, ...]


@dataclass(frozen=True)
class Plan:
    mode: str
    route: str
    group: str
    required_capabilities: tuple[str, ...]
    max_attempts: int
    prerequisites: tuple[str, ...]
    stages: tuple[Stage, ...]


class PlanError(ValueError):
    """A route cannot be resolved locally."""


def plan_route(config: Config, route_id: str) -> Plan:
    if route_id not in config.routes:
        raise PlanError('Unknown route')
    route = config.routes[route_id]
    accounts = tuple(account for account in config.groups[route.group].accounts
                     if config.accounts[account].enabled)
    models = route.models if route.fallback_on else route.models[:1]
    if not accounts:
        models = ()
    return Plan(
        'simulation', route_id, route.group, route.required_capabilities,
        config.limits.max_attempts,
        ('identity_verified', 'required_capabilities_verified_per_account_model',
         'health_cooldown_capacity_checked', 'pre_output_only'),
        tuple(Stage(model, accounts, () if index == 0 else route.fallback_on)
              for index, model in enumerate(models)),
    )

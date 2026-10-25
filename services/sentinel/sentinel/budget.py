"""External AI budget meter (spec §16). Disabled until a provider is explicitly configured by the admin."""
from __future__ import annotations

from dataclasses import dataclass, field


class BudgetDenied(Exception):
    pass


@dataclass
class ProviderBudget:
    provider: str
    enabled: bool = False
    monthly_cap_usd: float = 5.00
    spent_month_usd: float = 0.0
    escalations_by_issue: dict[str, int] = field(default_factory=dict)
    max_escalations_per_issue: int = 3


class BudgetMeter:
    def __init__(self, budgets: dict[str, ProviderBudget] | None = None):
        self.budgets = budgets or {}

    def authorize(self, provider: str, issue_key: str, est_cost_usd: float, local_attempts: int,
                  redaction_clean: bool, sensitive_upload_approved: bool = False, contains_sensitive: bool = False) -> None:
        b = self.budgets.get(provider)
        if b is None or not b.enabled:
            raise BudgetDenied(f"external provider {provider!r} is not enabled; configure it in Pat → Settings → External AI")
        if local_attempts < 3:
            raise BudgetDenied(f"only {local_attempts} local attempt(s); three local approaches are required before escalating")
        if b.escalations_by_issue.get(issue_key, 0) >= b.max_escalations_per_issue:
            raise BudgetDenied(f"issue {issue_key} already escalated {b.max_escalations_per_issue}×; report a stalemate")
        if b.spent_month_usd + est_cost_usd > b.monthly_cap_usd:
            raise BudgetDenied(f"would exceed monthly cap (${b.spent_month_usd:.2f} + ${est_cost_usd:.2f} > ${b.monthly_cap_usd:.2f})")
        if not redaction_clean:
            raise BudgetDenied("secret redaction report is not clean")
        if contains_sensitive and not sensitive_upload_approved:
            raise BudgetDenied("payload classified sensitive; needs explicit user approval (data.send_external)")

    def record(self, provider: str, issue_key: str, actual_cost_usd: float) -> None:
        b = self.budgets[provider]
        b.spent_month_usd += actual_cost_usd
        b.escalations_by_issue[issue_key] = b.escalations_by_issue.get(issue_key, 0) + 1

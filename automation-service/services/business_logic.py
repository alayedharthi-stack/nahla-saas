from models.models import AutomationRuleCreate, AutomationRuleResponse
from repositories.data_access import save_automation_rule, trigger_abandoned_cart_flow, get_automation_summary


def create_automation_rule(payload: AutomationRuleCreate) -> AutomationRuleResponse:
    saved = save_automation_rule(payload)
    return AutomationRuleResponse(**saved)


def trigger_abandoned_cart() -> dict:
    return trigger_abandoned_cart_flow()


def get_summary() -> dict:
    return get_automation_summary()

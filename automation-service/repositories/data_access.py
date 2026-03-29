from models.models import AutomationRuleCreate


def save_automation_rule(payload: AutomationRuleCreate) -> dict:
    return {
        "id": 1,
        "tenant_id": payload.tenant_id,
        "name": payload.name,
        "trigger_type": payload.trigger_type,
        "trigger_config": payload.trigger_config,
        "action_config": payload.action_config,
        "is_active": payload.is_active,
    }


def trigger_abandoned_cart_flow() -> dict:
    return {"status": "abandoned_cart_flow_triggered"}


def get_automation_summary() -> dict:
    return {"active_rules": 0, "pending_actions": 0}

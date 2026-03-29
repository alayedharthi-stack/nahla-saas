from models.models import CampaignCreate


def save_campaign(payload: CampaignCreate) -> dict:
    return {
        "id": 1,
        "tenant_id": payload.tenant_id,
        "name": payload.name,
        "trigger": payload.trigger,
        "actions": payload.actions,
        "is_active": payload.is_active,
    }


def get_campaign_performance() -> dict:
    return {"campaigns": [], "metrics": {"clicks": 0, "conversions": 0}}

from models.models import CampaignCreate, CampaignResponse
from repositories.data_access import save_campaign, get_campaign_performance


def create_campaign(payload: CampaignCreate) -> CampaignResponse:
    saved = save_campaign(payload)
    return CampaignResponse(**saved)


def get_performance() -> dict:
    return get_campaign_performance()

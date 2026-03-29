from models.models import MetricsResponse, CampaignPerformanceResponse, ConversationAnalyticsResponse
from repositories.data_access import get_metrics_data, get_campaign_performance_data, get_conversation_analytics_data


def get_store_metrics() -> MetricsResponse:
    return MetricsResponse(**get_metrics_data())


def get_campaign_performance() -> CampaignPerformanceResponse:
    return CampaignPerformanceResponse(**get_campaign_performance_data())


def get_conversation_analytics() -> ConversationAnalyticsResponse:
    return ConversationAnalyticsResponse(**get_conversation_analytics_data())

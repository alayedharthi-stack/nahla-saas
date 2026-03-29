from fastapi import APIRouter
from models.models import WidgetSettingsRequest, WidgetSettingsResponse
from services.business_logic import update_widget_settings as update_widget_settings_logic, get_widget_settings as get_widget_settings_logic

router = APIRouter(prefix="/widget", tags=["widget"])

@router.post("/settings", response_model=WidgetSettingsResponse)
async def update_widget_settings(payload: WidgetSettingsRequest):
    return update_widget_settings_logic(payload)

@router.get("/settings", response_model=WidgetSettingsResponse)
async def get_widget_settings():
    return get_widget_settings_logic()

from pydantic import BaseModel
from typing import Optional, Dict, Any

class WidgetSettingsRequest(BaseModel):
    tenant_id: int
    bot_name: Optional[str] = None
    logo_url: Optional[str] = None
    color: Optional[str] = None
    welcome_text: Optional[str] = None
    show_nahla_branding: Optional[bool] = None
    branding_text: Optional[str] = "🐝 Powered by Nahla"
    options: Optional[Dict[str, Any]] = None

class WidgetSettingsResponse(WidgetSettingsRequest):
    id: int

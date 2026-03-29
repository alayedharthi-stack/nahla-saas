from models.models import WidgetSettingsRequest, WidgetSettingsResponse
from repositories.data_access import save_widget_settings, get_widget_settings_data

DEFAULT_BRANDING_TEXT = "🐝 Powered by Nahla"


def update_widget_settings(payload: WidgetSettingsRequest) -> WidgetSettingsResponse:
    saved = save_widget_settings(payload)
    if saved.get("show_nahla_branding") is None:
        saved["show_nahla_branding"] = True
    if not saved.get("branding_text"):
        saved["branding_text"] = DEFAULT_BRANDING_TEXT
    return WidgetSettingsResponse(**saved)


def get_widget_settings() -> WidgetSettingsResponse:
    data = get_widget_settings_data()
    if data.get("show_nahla_branding") is None:
        data["show_nahla_branding"] = True
    if not data.get("branding_text"):
        data["branding_text"] = DEFAULT_BRANDING_TEXT
    return WidgetSettingsResponse(**data)

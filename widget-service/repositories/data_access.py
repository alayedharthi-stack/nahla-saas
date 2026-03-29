from models.models import WidgetSettingsRequest


def save_widget_settings(payload: WidgetSettingsRequest) -> dict:
    return {
        "id": 1,
        "tenant_id": payload.tenant_id,
        "bot_name": payload.bot_name,
        "logo_url": payload.logo_url,
        "color": payload.color,
        "welcome_text": payload.welcome_text,
        "show_nahla_branding": payload.show_nahla_branding,
        "branding_text": payload.branding_text,
        "options": payload.options,
    }


def get_widget_settings_data() -> dict:
    return {
        "id": 1,
        "tenant_id": 1,
        "bot_name": "Nahla Bot",
        "logo_url": "https://example.com/logo.png",
        "color": "#00aaff",
        "welcome_text": "Welcome to the store!",
        "show_nahla_branding": True,
        "branding_text": "🐝 Powered by Nahla",
        "options": {"show_powered_by": True},
    }

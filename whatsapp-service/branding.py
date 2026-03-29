from re import search
from typing import Dict, Any

DEFAULT_BRANDING_TEXT = "🐝 Powered by Nahla"
DEFAULT_BRANDING_CONFIG = {
    "show_nahla_branding": True,
    "branding_text": DEFAULT_BRANDING_TEXT,
}


def get_tenant_branding_config(tenant: str) -> Dict[str, Any]:
    # Placeholder implementation.
    # In a production SaaS setup, this should read tenant settings from a database
    # or a tenant settings service, and map plan state to branding enforcement.
    return DEFAULT_BRANDING_CONFIG.copy()


def is_welcome_input(message: str) -> bool:
    if not message:
        return False
    text = message.strip().lower()
    return bool(search(r"\b(hi|hello|hey|welcome|مرحبا|أهلا|اهلا|سلام)\b", text))


def apply_branding(response_text: str, branding_config: Dict[str, Any], force_footer: bool = False) -> str:
    if not branding_config.get("show_nahla_branding", True):
        return response_text
    brand_text = branding_config.get("branding_text") or DEFAULT_BRANDING_TEXT
    response = response_text.strip()
    footer = f"\n\n{brand_text}"
    if force_footer or brand_text not in response:
        return f"{response}{footer}"
    return response

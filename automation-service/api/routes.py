from fastapi import APIRouter
from models.models import AutomationRuleCreate, AutomationRuleResponse
from services.business_logic import create_automation_rule as create_automation_rule_logic, trigger_abandoned_cart as trigger_abandoned_cart_logic, get_summary as get_summary_logic

router = APIRouter(prefix="/automations", tags=["automations"])

@router.post("/rules", response_model=AutomationRuleResponse)
async def create_automation_rule(payload: AutomationRuleCreate):
    return create_automation_rule_logic(payload)

@router.post("/abandoned-cart")
async def trigger_abandoned_cart():
    return trigger_abandoned_cart_logic()

@router.get("/summary")
async def automation_summary():
    return get_summary_logic()

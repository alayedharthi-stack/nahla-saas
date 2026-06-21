
def test_fulfillment_location_routes_checkout_continuation_not_suspend():
    """Phase 3B — current-turn location answer during checkout stays checkout owner."""
    from modules.ai.brain.interpret.semantic_turn_interpreter import SemanticTurnInterpretation
    from modules.ai.brain.state.state_relevance import StateRelevanceVerdict
    from modules.ai.brain.turn.arbiter import arbitrate_turn
    from modules.ai.brain.turn.contract import OWNER_CHECKOUT
    from modules.ai.brain.turn.understanding import synthesize_turn_understanding
    from modules.ai.brain.types import BrainContext, CommerceFacts, Intent, MerchantConversationState, OrderPreparationState

    st = MerchantConversationState(turn=5, stage="checkout")
    st.order_prep = OrderPreparationState(product_id="p1", missing_fields=["city"])
    st.last_question_asked = "ما المدينة؟"
    st.last_question_answered = False

    ctx = BrainContext(
        tenant_id=1,
        customer_phone="+966500000000",
        message="أنا في الطائف",
        raw_message="أنا في الطائف",
        intent=Intent(name="social", confidence=0.85, slots={}),
        state=st,
        facts=CommerceFacts(has_products=True),
        history=[],
        state_relevance=StateRelevanceVerdict(
            safe_to_resume_state=True,
            detected_topic_shift=False,
            fulfillment_state_relevant=True,
            active_workflows=("active_fulfillment",),
        ),
        semantic_interpretation=SemanticTurnInterpretation(
            canonical_text="أنا في الطائف",
            interpreted_intent="fulfillment_location_update",
            context_anchor="active_order_context",
            confidence=0.88,
            commerce_frame="fulfillment",
        ),
    )

    understanding = synthesize_turn_understanding(ctx)
    arbitration = arbitrate_turn(understanding, ctx)

    assert understanding.current_intent == "checkout_continuation"
    assert understanding.should_suspend_stale_state is False
    assert arbitration.turn_owner == OWNER_CHECKOUT

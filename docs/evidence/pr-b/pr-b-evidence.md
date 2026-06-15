# PR-B Operational Evidence

Generated: `2026-06-15T07:52:05.001280+00:00`

## Runtime isolation (USE_STRUCTURED_BRANCH_CONTACTS=0)

**All checks pass:** `True`

- PASS — `branch_contact_evidence.structured_branch_contacts_enabled()`
- PASS — `location_link_policy / lookup_structured_maps_url`
- PASS — `arrival_contact_delivery / resolve_reception_contact`
- PASS — `staff_contact_registry / load_structured_staff_contact_registry`
- PASS — `escalation_chain / load_structured_escalation_chain`
- PASS — `staff_contact_registry KB fallback`
- PASS — `arrival_contact_delivery resolve_arrival_contact_evidence`

## Multi-branch validation

### فرع الرياض (الرياض)
- maps: `https://maps.google.com/?q=riyadh-showroom`
- active: `True`
- default reception: `استقبال الرياض` / `+966501111111`
- escalation:
  - L1: بائع الرياض (+966504111111)
  - L2: إدارة الرياض (+966504111112)

### فرع جدة (جدة)
- maps: `https://maps.google.com/?q=jeddah-showroom`
- active: `True`
- default reception: `استقبال جدة` / `+966502222221`
- escalation:
  - L1: بائع جدة (+966504222221)
  - L2: CS جدة (+966504222222)

### فرع الطائف (الطائف)
- maps: `https://maps.google.com/?q=taif-showroom`
- active: `False`
- default reception: `استقبال الطائف` / `+966503333331`
- escalation:
  - L1: بائع الطائف (+966504333331)
  - L2: إدارة الطائف (+966504333332)

# Nahla SaaS Architecture

This document describes the Nahla SaaS commerce platform architecture.

## Core Services
- backend
- ai-engine
- whatsapp-service
- catalog-service
- order-service
- coupon-service
- campaign-service
- widget-service
- conversation-service
- automation-service
- analytics-service
- location-service

## Integrations
- integrations/salla
- integrations/zid
- integrations/shopify

## Shared Services
- services/ai-orchestrator
- services/message-router
- services/integration-manager

## Key Platform Capabilities
- Multi-tenant store architecture
- Store-level WhatsApp Cloud API configuration
- Product catalog sync
- Order sync and checkout links
- Customer sync and address normalization
- Coupon generation and store-specific coupon policy
- Campaign automation and abandoned cart flows
- Widget branding and store-specific settings
- Store knowledge base and AI policy controls
- Human handoff and urgent conversation support
- Audit logs for coupon decisions, AI actions, message events, webhooks, and admin overrides
- Mobile-ready API-first backend for dashboard, iOS, and Android

## Store and Delivery Settings
- store address
- Google Maps link
- Apple Maps link
- delivery zones
- shipping fees by city/zone
- same-day delivery support on/off
- pickup option on/off
- store branding and widget personalization

## Customer Address and Location Storage
- raw address text
- Saudi national address
- Google Maps link
- Apple Maps link
- WhatsApp shared location
- normalized lat/lng
- city
- district

## Recommendation and AI Controls
- related product recommendations
- upsell suggestions
- bundle suggestions
- product image and link sending
- store-specific knowledge base policies
- allowed/blocked answer categories
- escalation to human rules
- owner override instructions

## Audit and Safety
- coupon decision logs
- AI action logs
- message event logs
- webhook logs
- admin override logs

## Mobile Readiness
- API-first backend designed for web dashboard, iOS, and Android
- push notifications support
- owner approvals and coupon override flows
- conversation monitoring
- AI on/off controls
- order monitoring

## Database Models
- Tenant
- User
- WhatsAppNumber
- Product
- Order
- Coupon
- CouponRule
- Integration
- SyncLog
- AutomationRule
- DeliveryZone
- ShippingFee
- Customer
- CustomerAddress
- KnowledgePolicy
- Conversation
- MessageEvent
- WidgetSetting
- AuditLog

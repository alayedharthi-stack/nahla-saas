/**
 * catalogIntelligence.ts
 * ──────────────────────
 * Typed client for Catalog Intelligence Phase 1 APIs (groups, relations, rankings).
 */
import { apiCall } from './client'

export interface CatalogIntelligenceSettings {
  best_seller_mode: string
  max_relations_per_product: number
  default_group_slug: string
  small_catalog_threshold: number
  scoring_weights: Record<string, number>
}

export interface ProductGroup {
  id: number
  slug: string
  label: string
  description: string
  catalog_match: string
  priority: number
  is_active: boolean
  source: string
  metadata_json: Record<string, unknown>
  product_count: number
  items?: ProductGroupItem[]
}

export interface ProductGroupItem {
  id: number
  product_id: number
  variant_id: number | null
  priority: number
  label_override: string
  product_title: string
}

export interface ProductRelation {
  id: number
  source_product_id: number
  target_product_id: number
  relation_type: string
  priority: number
  source: string
  target_product_title: string
}

export interface ProductRanking {
  product_id: number
  is_best_seller: boolean
  sales_rank: number | null
  sales_score: number | null
  merchant_priority: number
  stats_source: string
  updated_at: string
}

export interface ProductGroupInput {
  slug?: string
  label: string
  description?: string
  catalog_match?: string
  priority?: number
  is_active?: boolean
  metadata_json?: Record<string, unknown>
}

export interface GroupItemInput {
  product_id: number
  variant_id?: number | null
  priority?: number
  label_override?: string
}

export interface ProductRelationInput {
  target_product_id: number
  relation_type: string
  priority?: number
  source?: string
}

export interface CatalogValidationIssue {
  severity: 'error' | 'warning' | 'info'
  code: string
  message: string
  context: Record<string, unknown>
}

export interface CatalogValidationReport {
  ok: boolean
  ready: boolean
  summary: {
    active_groups: number
    total_groups: number
    grouped_products: number
    uncategorized_products: number
    best_sellers: number
    errors: number
    warnings: number
    info: number
  }
  issues: CatalogValidationIssue[]
}

export const catalogIntelligenceApi = {
  getSettings(): Promise<{ tenant_id: number; catalog_intelligence: CatalogIntelligenceSettings }> {
    return apiCall('/settings/catalog-intelligence')
  },

  getValidation(): Promise<{ tenant_id: number } & CatalogValidationReport> {
    return apiCall('/catalog-intelligence/validation')
  },

  saveSettings(body: Partial<CatalogIntelligenceSettings>): Promise<{ status: string; catalog_intelligence: CatalogIntelligenceSettings }> {
    return apiCall('/settings/catalog-intelligence', {
      method: 'PUT',
      body: JSON.stringify(body),
    })
  },

  listGroups(includeInactive = false): Promise<{ tenant_id: number; groups: ProductGroup[] }> {
    const qs = includeInactive ? '?include_inactive=true' : ''
    return apiCall(`/catalog-intelligence/groups${qs}`)
  },

  getGroup(groupId: number): Promise<{ tenant_id: number; group: ProductGroup }> {
    return apiCall(`/catalog-intelligence/groups/${groupId}`)
  },

  createGroup(body: ProductGroupInput): Promise<{ status: string; group: ProductGroup }> {
    return apiCall('/catalog-intelligence/groups', {
      method: 'POST',
      body: JSON.stringify(body),
    })
  },

  updateGroup(groupId: number, body: Partial<ProductGroupInput>): Promise<{ status: string; group: ProductGroup }> {
    return apiCall(`/catalog-intelligence/groups/${groupId}`, {
      method: 'PATCH',
      body: JSON.stringify(body),
    })
  },

  deleteGroup(groupId: number): Promise<{ status: string }> {
    return apiCall(`/catalog-intelligence/groups/${groupId}`, { method: 'DELETE' })
  },

  reorderGroups(groupIds: number[]): Promise<{ status: string; groups: ProductGroup[] }> {
    return apiCall('/catalog-intelligence/groups/reorder', {
      method: 'POST',
      body: JSON.stringify({ group_ids: groupIds }),
    })
  },

  addGroupItem(groupId: number, body: GroupItemInput): Promise<{ status: string; item: ProductGroupItem }> {
    return apiCall(`/catalog-intelligence/groups/${groupId}/items`, {
      method: 'POST',
      body: JSON.stringify(body),
    })
  },

  deleteGroupItem(groupId: number, itemId: number): Promise<{ status: string }> {
    return apiCall(`/catalog-intelligence/groups/${groupId}/items/${itemId}`, { method: 'DELETE' })
  },

  listRelations(productId: number, relationType = ''): Promise<{ tenant_id: number; relations: ProductRelation[] }> {
    const qs = relationType ? `?relation_type=${encodeURIComponent(relationType)}` : ''
    return apiCall(`/catalog-intelligence/products/${productId}/relations${qs}`)
  },

  createRelation(productId: number, body: ProductRelationInput): Promise<{ status: string; relation: ProductRelation }> {
    return apiCall(`/catalog-intelligence/products/${productId}/relations`, {
      method: 'POST',
      body: JSON.stringify(body),
    })
  },

  deleteRelation(productId: number, relationId: number): Promise<{ status: string }> {
    return apiCall(`/catalog-intelligence/products/${productId}/relations/${relationId}`, { method: 'DELETE' })
  },

  getRanking(productId: number): Promise<{ tenant_id: number; ranking: ProductRanking }> {
    return apiCall(`/catalog-intelligence/products/${productId}/ranking`)
  },

  saveRanking(productId: number, body: Partial<ProductRanking>): Promise<{ status: string; ranking: ProductRanking }> {
    return apiCall(`/catalog-intelligence/products/${productId}/ranking`, {
      method: 'PUT',
      body: JSON.stringify(body),
    })
  },
}

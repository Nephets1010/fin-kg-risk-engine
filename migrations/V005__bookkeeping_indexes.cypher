// ---------------------------------------------------------------------------
// Migration 005 — bookkeeping indexes
// ---------------------------------------------------------------------------

CREATE INDEX entity_created_at IF NOT EXISTS
FOR (n:Entity) ON (n.createdAt);

CREATE INDEX relation_type IF NOT EXISTS
FOR ()-[r]-() ON (r.relationType);

MERGE (m:SchemaMigration {version: 5})
SET m.description = 'bookkeeping indexes',
    m.appliedAt = datetime();

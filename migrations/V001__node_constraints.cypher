// ---------------------------------------------------------------------------
// Migration 001 — node constraints and labels
//
// Applied by `fin-kg migrate` or by the API on startup (ensure_schema).
// Every statement is idempotent so re-running the migration is a no-op.
// ---------------------------------------------------------------------------

// Unique identity of every node in the graph.
CREATE CONSTRAINT entity_id IF NOT EXISTS
FOR (n:Entity) REQUIRE n.id IS UNIQUE;

CREATE CONSTRAINT company_id IF NOT EXISTS
FOR (n:Company) REQUIRE n.id IS UNIQUE;

CREATE CONSTRAINT person_id IF NOT EXISTS
FOR (n:Person) REQUIRE n.id IS UNIQUE;

CREATE CONSTRAINT event_id IF NOT EXISTS
FOR (n:Event) REQUIRE n.id IS UNIQUE;

CREATE CONSTRAINT product_id IF NOT EXISTS
FOR (n:Product) REQUIRE n.id IS UNIQUE;

// Record the applied migration so `ensure_schema` can skip work already done.
MERGE (m:SchemaMigration {version: 1})
SET m.description = 'node constraints and labels',
    m.appliedAt = datetime();

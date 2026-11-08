// ---------------------------------------------------------------------------
// Migration 002 — credit code uniqueness (the entity-alignment anchor)
//
// A unified social credit code identifies exactly one legal entity. Making it
// unique lets the ingestion pipeline reject records that would create a
// duplicate company, and lets Neo4j reject them as a last line of defence.
// ---------------------------------------------------------------------------

CREATE CONSTRAINT company_credit_code IF NOT EXISTS
FOR (n:Company) REQUIRE n.creditCode IS UNIQUE;

MERGE (m:SchemaMigration {version: 2})
SET m.description = 'credit code uniqueness',
    m.appliedAt = datetime();

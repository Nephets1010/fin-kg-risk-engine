// ---------------------------------------------------------------------------
// Migration 004 — lookup and risk indexes
// ---------------------------------------------------------------------------

CREATE INDEX company_name IF NOT EXISTS
FOR (n:Company) ON (n.name);

CREATE INDEX person_name IF NOT EXISTS
FOR (n:Person) ON (n.name);

CREATE INDEX product_name IF NOT EXISTS
FOR (n:Product) ON (n.name);

CREATE INDEX company_risk_score IF NOT EXISTS
FOR (n:Company) ON (n.riskScore);

CREATE INDEX company_industry IF NOT EXISTS
FOR (n:Company) ON (n.industry);

MERGE (m:SchemaMigration {version: 4})
SET m.description = 'lookup and risk indexes',
    m.appliedAt = datetime();

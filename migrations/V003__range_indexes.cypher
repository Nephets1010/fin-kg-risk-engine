// ---------------------------------------------------------------------------
// Migration 003 — range indexes for the propagation and indicator queries
//
// hold_percent       : accelerates "who holds more than X% of this company"
// guarantee_amount   : accelerates exposure aggregation
// event_occurred_date: accelerates the 12-month indicator window scans
// ---------------------------------------------------------------------------

CREATE INDEX hold_percent IF NOT EXISTS
FOR ()-[r:HOLD]->() ON (r.percent);

CREATE INDEX guarantee_amount IF NOT EXISTS
FOR ()-[r:GUARANTEE]->() ON (r.guaranteed_amount);

CREATE INDEX event_occurred_date IF NOT EXISTS
FOR (n:Event) ON (n.occurredDate);

CREATE INDEX event_type IF NOT EXISTS
FOR (n:Event) ON (n.eventType);

MERGE (m:SchemaMigration {version: 3})
SET m.description = 'propagation and indicator range indexes',
    m.appliedAt = datetime();

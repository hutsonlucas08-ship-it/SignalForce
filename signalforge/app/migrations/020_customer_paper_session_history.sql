-- Track the start of each automated paper session so its dashboard history is session-specific.
ALTER TABLE customer_paper_accounts ADD COLUMN auto_started_at TEXT;

-- Store newer customer strategy variants without rewriting the original mode constraint.
ALTER TABLE customer_paper_accounts ADD COLUMN strategy_profile TEXT;

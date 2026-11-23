ALTER TABLE paper_bot_state ADD COLUMN run_started_at TEXT;
ALTER TABLE paper_bot_state ADD COLUMN run_until TEXT;
ALTER TABLE paper_bot_state ADD COLUMN session_seconds INTEGER;
UPDATE paper_bot_state SET enabled=0, run_started_at=NULL, run_until=NULL, session_seconds=NULL;

-- A database lease prevents duplicate customer paper-trader workers across web instances.
CREATE TABLE customer_paper_bot_state (
    id INTEGER PRIMARY KEY CHECK(id=1),
    lock_owner TEXT,
    lock_until_epoch INTEGER NOT NULL DEFAULT 0
);
INSERT INTO customer_paper_bot_state(id,lock_owner,lock_until_epoch)
VALUES(1,NULL,0)
ON CONFLICT(id) DO NOTHING;

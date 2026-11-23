-- Existing beta registrations were previously held pending manual activation.
-- Self-service beta access is now immediate, so allow those already-registered users in.
UPDATE customer_accounts
SET status='active', updated_at=CURRENT_TIMESTAMP
WHERE status='pending';

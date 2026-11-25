-- Email confirmation is no longer required for customer accounts.
-- Activate accounts created under the previous verification flow so they can sign in.
UPDATE customer_accounts
SET status='active', updated_at=CURRENT_TIMESTAMP
WHERE status='pending';

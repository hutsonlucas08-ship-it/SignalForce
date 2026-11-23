ALTER TABLE tracked_wallets ADD COLUMN helius_status TEXT NOT NULL DEFAULT 'active';
ALTER TABLE tracked_wallets ADD COLUMN helius_404_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE tracked_wallets ADD COLUMN helius_last_error TEXT;

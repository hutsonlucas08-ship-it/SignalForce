import asyncio

import httpx

from app.db import connect, run_migrations, utcnow
from app.sources.helius_worker import sync_once

WALLET = "11111111111111111111111111111111"
MINT = "So11111111111111111111111111111111111111112"


class FakeHelius:
    def __init__(self, payload=None, status=200):
        self.payload = [] if payload is None else payload
        self.status = status
        self.calls = []

    async def get(self, url, params):
        self.calls.append((url, params))
        request = httpx.Request("GET", url)
        response = httpx.Response(self.status, request=request, json=self.payload)
        response.raise_for_status()
        return response


def setup_db(path, with_wallet=True):
    conn = connect(str(path))
    run_migrations(conn)
    if with_wallet:
        conn.execute(
            "INSERT INTO tracked_wallets(wallet_address,label,added_at) VALUES(?,?,?)",
            (WALLET, "sample", utcnow()),
        )
        conn.commit()
    conn.close()


def test_sync_once_polls_watchlist_and_persists_swap(tmp_path):
    async def run():
        db = tmp_path / "worker.db"
        setup_db(db)
        fake = FakeHelius([{
            "signature": "worker-sig", "timestamp": 1790000000,
            "tokenTransfers": [{"mint": MINT, "tokenAmount": 10,
                                "fromUserAccount": "pool", "toUserAccount": WALLET}],
            "nativeTransfers": [{"amount": 500000000, "fromUserAccount": WALLET,
                                 "toUserAccount": "pool"}],
        }])
        result = await sync_once(str(db), "test-key", client=fake)
        assert result["wallets_checked"] == 1
        assert result["transactions_inserted"] == 1
        assert result["wallet_errors"] == 0
        assert fake.calls[0][1]["type"] == "SWAP"
        conn = connect(str(db))
        assert conn.execute("SELECT COUNT(*) FROM wallet_trade_events").fetchone()[0] == 1
        assert conn.execute("SELECT last_signature FROM tracked_wallets").fetchone()[0] == "worker-sig"
        conn.close()
    asyncio.run(run())


def test_sync_once_deduplicates_repeated_poll(tmp_path):
    async def run():
        db = tmp_path / "dedupe.db"
        setup_db(db)
        fake = FakeHelius([{
            "signature": "repeat-sig", "timestamp": 1790000000,
            "tokenTransfers": [{"mint": MINT, "tokenAmount": 2,
                                "fromUserAccount": "pool", "toUserAccount": WALLET}],
            "nativeTransfers": [],
        }])
        first = await sync_once(str(db), "test-key", client=fake)
        second = await sync_once(str(db), "test-key", client=fake)
        assert first["transactions_inserted"] == 1
        assert second["transactions_inserted"] == 0
        assert second["duplicates"] == 1
    asyncio.run(run())


def test_sync_once_records_provider_failure(tmp_path):
    async def run():
        db = tmp_path / "failure.db"
        setup_db(db)
        result = await sync_once(str(db), "test-key", client=FakeHelius(status=401))
        assert result["wallet_errors"] == 1
        conn = connect(str(db))
        source = conn.execute(
            "SELECT consecutive_failures FROM data_sources WHERE name=?",
            ("helius_wallet_sync",),
        ).fetchone()
        assert source[0] == 1
        conn.close()
    asyncio.run(run())


def test_sync_once_with_empty_watchlist_is_healthy(tmp_path):
    async def run():
        db = tmp_path / "empty.db"
        setup_db(db, with_wallet=False)
        result = await sync_once(str(db), "test-key", client=FakeHelius())
        assert result["wallets_checked"] == 0
        assert result["wallet_errors"] == 0
    asyncio.run(run())

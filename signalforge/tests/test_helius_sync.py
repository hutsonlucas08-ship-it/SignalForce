import pytest

from app.db import connect, run_migrations, utcnow
from app.sources.helius_sync import parse_wallet_swaps, store_wallet_transactions

WALLET = "11111111111111111111111111111111"
MINT = "So11111111111111111111111111111111111111112"


def _tx(**overrides):
    tx = {
        "signature": "sig-1",
        "timestamp": 1790000000,
        "tokenTransfers": [{
            "mint": MINT,
            "tokenAmount": 25.5,
            "fromUserAccount": "market-maker",
            "toUserAccount": WALLET,
        }],
        "nativeTransfers": [{
            "amount": 1_000_000_000,
            "fromUserAccount": WALLET,
            "toUserAccount": "market-maker",
        }],
    }
    tx.update(overrides)
    return tx


def test_parse_incoming_token_as_buy_and_sol_spend():
    events = parse_wallet_swaps(WALLET, [_tx()])
    assert len(events) == 1
    assert events[0]["side"] == "buy"
    assert events[0]["token_amount"] == 25.5
    assert events[0]["sol_amount"] == 1.0
    assert events[0]["price_usd"] is None


def test_parse_outgoing_token_as_sell_and_sol_received():
    tx = _tx(tokenTransfers=[{
        "mint": MINT, "tokenAmount": 3,
        "fromUserAccount": WALLET, "toUserAccount": "market-maker",
    }], nativeTransfers=[{
        "amount": 250_000_000,
        "fromUserAccount": "market-maker", "toUserAccount": WALLET,
    }])
    events = parse_wallet_swaps(WALLET, [tx])
    assert events[0]["side"] == "sell"
    assert events[0]["sol_amount"] == 0.25


def test_ignore_unrelated_or_malformed_transfers():
    tx = _tx(tokenTransfers=[
        {"mint": MINT, "tokenAmount": 1, "fromUserAccount": "a", "toUserAccount": "b"},
        {"mint": "", "tokenAmount": 1, "toUserAccount": WALLET},
        {"mint": MINT, "tokenAmount": "bad", "toUserAccount": WALLET},
    ])
    assert parse_wallet_swaps(WALLET, [tx]) == []


def test_ignore_transactions_without_signature_or_timestamp():
    assert parse_wallet_swaps(WALLET, [{"tokenTransfers": []}]) == []


def test_store_is_idempotent_and_requires_tracked_wallet(tmp_path):
    conn = connect(str(tmp_path / "sync.db"))
    run_migrations(conn)
    conn.execute(
        "INSERT INTO tracked_wallets(wallet_address, label, added_at) VALUES (?, ?, ?)",
        (WALLET, "test", utcnow()),
    )
    conn.commit()
    first = store_wallet_transactions(conn, WALLET, [_tx()])
    second = store_wallet_transactions(conn, WALLET, [_tx()])
    assert first == {"parsed": 1, "inserted": 1, "duplicates": 0}
    assert second == {"parsed": 1, "inserted": 0, "duplicates": 1}
    assert conn.execute("SELECT COUNT(*) FROM wallet_trade_events").fetchone()[0] == 1
    conn.close()


def test_reject_blank_wallet_address():
    with pytest.raises(ValueError):
        parse_wallet_swaps("", [])

from app.j7_feed import _normalize, _contracts, _upsert, snapshot


SOL_MINT = "TokenMint1111111111111111111111111111111111"


def test_normalizes_x_post_and_safe_post_url():
    item = _normalize({
        "id": "123456789",
        "createdAt": 1764000000000,
        "author": {"handle": "alpha", "name": "Alpha", "verified": True},
        "text": "New token " + SOL_MINT,
        "tweetUrl": "https://x.com/alpha/status/123456789",
        "contractAddress": SOL_MINT,
        "extraContractAddresses": ["invalid"],
    })
    assert item["platform"] == "X"
    assert item["url"] == "https://x.com/alpha/status/123456789"
    assert item["contracts"] == [SOL_MINT]


def test_rejects_untrusted_post_url_and_non_solana_contracts():
    item = _normalize({
        "id": "123456789",
        "author": {"handle": "alpha"},
        "text": "hello",
        "tweetUrl": "https://phishing.example/alpha/status/123",
        "contractAddress": "0x0000000000000000000000000000000000000000",
    })
    assert item["url"] == "https://x.com/alpha/status/123456789"
    assert item["contracts"] == []


def test_token_meta_can_attach_to_existing_post():
    from app import j7_feed
    j7_feed._EVENTS.clear()
    j7_feed._PENDING_META.clear()
    _upsert({"id": "123456789", "author": {"handle": "alpha"}, "text": "mint"})
    j7_feed._token_meta({"id": "123456789", "mint": SOL_MINT, "tokenMeta": {"symbol": "ABC"}})
    assert snapshot()["events"][0]["contracts"] == [SOL_MINT]

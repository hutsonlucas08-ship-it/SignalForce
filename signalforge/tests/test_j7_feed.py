from app.j7_feed import _normalize, _contracts, _upsert, snapshot


SOL_MINT = "TokenMint1111111111111111111111111111111111"


def test_normalizes_x_post_and_safe_post_url():
    item = _normalize({
        "id": "123456789",
        "createdAt": 1764000000000,
        "author": {"handle": "elonmusk", "name": "Elon Musk", "verified": True},
        "text": "New token " + SOL_MINT,
        "tweetUrl": "https://x.com/elonmusk/status/123456789",
        "contractAddress": SOL_MINT,
        "extraContractAddresses": ["invalid"],
    })
    assert item["platform"] == "X"
    assert item["url"] == "https://x.com/elonmusk/status/123456789"
    assert item["contracts"] == [SOL_MINT]


def test_rejects_untrusted_post_url_and_non_solana_contracts():
    item = _normalize({
        "id": "123456789",
        "author": {"handle": "elonmusk"},
        "text": "hello",
        "tweetUrl": "https://phishing.example/elonmusk/status/123",
        "contractAddress": "0x0000000000000000000000000000000000000000",
    })
    assert item["url"] == "https://x.com/elonmusk/status/123456789"
    assert item["contracts"] == []


def test_token_meta_can_attach_to_existing_post():
    from app import j7_feed
    j7_feed._EVENTS.clear()
    j7_feed._PENDING_META.clear()
    _upsert({"id": "123456789", "author": {"handle": "elonmusk"}, "text": "mint"})
    j7_feed._token_meta({"id": "123456789", "mint": SOL_MINT, "tokenMeta": {"symbol": "ABC"}})
    assert snapshot()["events"][0]["contracts"] == [SOL_MINT]



def test_imported_axiom_watchlist_has_all_three_event_types_enabled():
    from app import j7_feed
    summary = j7_feed.watchlist_summary()
    assert summary["account_count"] == 192
    assert summary["tweets_enabled"] == 192
    assert summary["profile_updates_enabled"] == 192
    assert summary["follows_enabled"] == 192


def test_x_posts_are_limited_to_imported_watchlist():
    assert _normalize({
        "id": "987654321",
        "author": {"handle": "elonmusk"},
        "text": "hello from a watched account",
    }) is not None
    assert _normalize({
        "id": "987654322",
        "author": {"handle": "not_in_watchlist"},
        "text": "not on the imported list",
    }) is None

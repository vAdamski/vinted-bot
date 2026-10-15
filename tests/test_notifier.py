from vinted_bot.notifier import build_discord_embed, format_deal_text
from vinted_bot.pricing import MarketStats
from vinted_bot.scraper import Item, Seller

ITEM = Item(id=1, title="Nike Air Max 90", price=150.0, currency="PLN", total_price=158.2,
            url="https://www.vinted.pl/items/1-nike", photo_url="https://img/a.webp", seller_id=7,
            seller_login="ania_*x*", brand="Nike", details="42 · Bardzo dobry")
STATS = MarketStats(median=300.0, sample_size=96, used_size=90, low=0, high=900, computed_at=0)
SELLER = Seller(id=7, login="ania_*x*", rating=4.9, reviews=120)


def test_discord_embed():
    embed = build_discord_embed(ITEM, STATS, SELLER, "Air Max")
    assert embed["url"] == ITEM.url and embed["image"]["url"] == ITEM.photo_url
    values = {f["name"]: f["value"] for f in embed["fields"]}
    assert values["📉 Taniej"] == "**50%**"
    assert "ania\\_\\*x\\*" in values["⭐ Sprzedający"]


def test_embed_converts_to_discord_embed():
    import discord
    embed = discord.Embed.from_dict(build_discord_embed(ITEM, STATS, SELLER, "Air Max"))
    assert embed.image.url == ITEM.photo_url and embed.timestamp is not None


def test_text_format():
    text = format_deal_text(ITEM, STATS, SELLER, "Air Max")
    assert "50% taniej" in text and ITEM.url in text

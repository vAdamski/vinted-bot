import pytest

from vinted_bot import ConfigError, SearchConfig, load_config
from vinted_bot import Bot
from vinted_bot import build_discord_embed
from vinted_bot import MarketStats, item_search, title_matches, title_query
from vinted_bot import Item, Seller


def item(id_, title, price):
    return Item(id=id_, title=title, price=price, currency="PLN", total_price=None, url=f"https://v/items/{id_}",
                photo_url=None, seller_id=1, seller_login="s", brand=None, details=None)


class FakeVinted:
    """Kategoria: nowe oferty to telefon za 900 i słuchawki za 300.
    Rynek: telefony ~2000, słuchawki ~300."""

    def __init__(self):
        self.listing = []
        self.queries = []

    def search(self, search, limit=30, order="newest_first", with_price_filter=True):
        self.queries.append(search.query)
        if order == "newest_first" and with_price_filter:
            return self.listing
        if "telefon" in search.query:
            phones = [item(1000 + i, "Samsung Galaxy S24 telefon 128GB", 1900 + i * 10) for i in range(20)]
            cases = [item(1100 + i, "Etui Samsung Galaxy S24", 30) for i in range(20)]  # odpadają w filtrze
            return phones + cases
        if "słuchawki" in search.query:
            return [item(2000 + i, "Słuchawki Sony XM5", 290 + i) for i in range(20)]
        return [item(3000 + i, "x", 100 + i * 50) for i in range(30)]  # „mediana kategorii”

    def get_seller(self, user_id):
        return Seller(id=user_id, login="s", rating=5.0, reviews=50)

    def reset_session(self):
        pass


class CollectNotifier:
    def __init__(self):
        self.sent = []

    def send_deal(self, item, stats, seller, search):
        self.sent.append((item.title, stats))


@pytest.fixture
def bot(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(f"storage:\n  db_path: {tmp_path / 'db.sqlite'}\nlogging:\n  file: null\nsearches: []\n")
    cfg = load_config(cfg_path, env_file=tmp_path / "none.env")
    notifier = CollectNotifier()
    b = Bot(cfg, notifier)
    fake = FakeVinted()
    b.client = b.pricer.client = b.sellers.client = fake
    yield b, fake, notifier
    b.close()


def run(bot_tuple, search):
    b, fake, notifier = bot_tuple
    fake.listing = []
    b.process_search(search)  # pierwsze uruchomienie – cichy start
    fake.listing = [item(1, "Telefon Samsung Galaxy S24 NOWY", 900), item(2, "Słuchawki Sony XM5 nowe", 300)]
    b.process_search(search)
    return notifier.sent


def test_mode_all_sends_everything(bot):
    sent = run(bot, SearchConfig(name="e", catalog_ids=[2994]))  # bez frazy => all
    assert [t for t, _ in sent] == ["Słuchawki Sony XM5 nowe", "Telefon Samsung Galaxy S24 NOWY"]
    assert all(stats is None for _, stats in sent)


def test_mode_per_item_finds_real_deal(bot):
    sent = run(bot, SearchConfig(name="e", catalog_ids=[2994], mode="per_item"))
    # telefon 900 vs ~2000 => okazja; słuchawki 300 vs ~300 => nie
    assert [t for t, _ in sent] == ["Telefon Samsung Galaxy S24 NOWY"]
    assert sent[0][1].basis == "title" and sent[0][1].query == "telefon samsung galaxy s24"


def test_per_item_limit(bot):
    b, fake, notifier = bot
    b.cfg.pricing.per_item_max_per_cycle = 1
    sent = run(bot, SearchConfig(name="e", catalog_ids=[2994], mode="per_item"))
    assert sent == []  # pierwsza wyceniona (słuchawki – nie okazja), telefon pominięty przez limit


def test_per_item_without_comparable_offers_sends_nothing(bot):
    b, fake, notifier = bot
    fake.listing = []
    search = SearchConfig(name="e", catalog_ids=[2994], mode="per_item")
    b.process_search(search)
    fake.listing = [item(5, "Konsola Retro XYZ-9000", 100)]  # rynek nie zna tego modelu
    b.process_search(search)
    assert notifier.sent == []


@pytest.mark.parametrize("query,title,expected", [
    ("glosnik jbl flip 7", "Głośnik JBL Flip 7 nowy", True),
    ("glosnik jbl flip 7", "JBL Flip7 czarny", True),        # model sklejony
    ("glosnik jbl flip 7", "JBL Flip 6", False),             # inny model
    ("glosnik jbl flip 7", "Etui na JBL Flip 7", False),     # akcesorium
    ("etui jbl flip 7", "Etui na JBL Flip 7", True),         # chyba że szukamy etui
    ("golarka braun s9", "Pardel braun s9 pro", True),
    ("golarka braun s9", "Braun Series 9 Pro+", False),
])
def test_title_matches(query, title, expected):
    assert title_matches(query, title) is expected


def test_title_query_and_item_search():
    assert title_query("Słuchawki Sony WH-1000XM5 NOWE z metką!") == "słuchawki sony wh-1000xm5"
    s = item_search(SearchConfig(name="a", catalog_ids=[1], size_ids=[2], status_ids=[6], price_min=200), "x")
    assert s.query == "x" and s.catalog_ids == [1] and s.status_ids == [6] and s.size_ids == [] and s.price_min is None


def test_embed_without_stats():
    e = build_discord_embed(item(1, "Telefon", 900), None, Seller(1, "s", 5, 10), "e")
    assert e["title"].startswith("🆕") and [f["name"] for f in e["fields"]][0] == "💰 Cena"
    assert not any("Mediana" in f["name"] for f in e["fields"])


def test_title_basis_label():
    stats = MarketStats(2000, 20, 20, 0, 0, 0, basis="title", query="telefon samsung")
    e = build_discord_embed(item(1, "Telefon", 900), stats, Seller(1, "s", 5, 10), "e")
    assert any("podobnych" in f["name"] for f in e["fields"]) and "telefon samsung" in e["footer"]["text"]


def test_effective_mode_and_yaml_validation(tmp_path):
    assert SearchConfig(name="a", query="x").effective_mode == "deals"
    assert SearchConfig(name="a", catalog_ids=[1]).effective_mode == "all"
    p = tmp_path / "c.yaml"
    p.write_text("searches:\n  - name: a\n    catalog_ids: [1]\n    mode: zle\n")
    with pytest.raises(ConfigError):
        load_config(p, env_file=tmp_path / "none.env")

import pytest

from vinted_bot.config import SearchConfig
from vinted_bot.search_service import SearchError, SearchService, describe, parse_ids
from vinted_bot.storage import Storage


@pytest.fixture
def service(tmp_path):
    storage = Storage(str(tmp_path / "t.db"))
    yield SearchService(storage, [SearchConfig(name="Z yaml", query="lego")], max_searches=2)
    storage.close()


def s(name, **kw):
    return SearchConfig(name=name, query=kw.pop("query", "air max"), **kw)


def test_add_and_active(service):
    service.add(s("Air", catalog_ids=[1242], channel_id=99, deal_threshold=0.7, attributes={"color": [1]}), "adam")
    active = service.active()
    assert [x.name for x in active] == ["Z yaml", "Air"]
    air = active[1]
    assert air.source == "discord" and air.channel_id == 99 and air.catalog_ids == [1242]
    assert air.deal_threshold == 0.7 and air.attributes == {"color": [1]}


def test_name_conflicts_and_limit(service):
    with pytest.raises(SearchError):
        service.add(s("z YAML"))  # kolizja z YAML, bez względu na wielkość liter
    service.add(s("A"))
    with pytest.raises(SearchError):
        service.add(s("a"))
    service.add(s("B"))
    with pytest.raises(SearchError, match="limit"):
        service.add(s("C"))


def test_validation(service):
    with pytest.raises(SearchError):
        service.add(SearchConfig(name="pusty", query=""))
    with pytest.raises(SearchError):
        service.add(s("ceny", price_min=100, price_max=50))
    with pytest.raises(SearchError):
        service.add(s("x" * 51))


def test_pause_resume_resets_state(service):
    service.add(s("A"))
    service.storage.set_initialized("A")
    service.set_enabled("A", False)
    assert [x.name for x in service.active()] == ["Z yaml"]
    with pytest.raises(SearchError):
        service.set_enabled("A", False)
    service.set_enabled("A", True)
    assert "A" in [x.name for x in service.active()]
    assert not service.storage.is_initialized("A")  # po wznowieniu cichy start


def test_remove(service):
    service.add(s("A"))
    service.storage.set_initialized("A")
    service.remove("A")
    assert [x.name for x in service.active()] == ["Z yaml"]
    assert not service.storage.is_initialized("A")
    with pytest.raises(SearchError):
        service.remove("A")
    with pytest.raises(SearchError, match="config.yaml"):
        service.remove("Z yaml")


def test_parse_ids_and_describe():
    assert parse_ids("1242, 1243 1242", "k") == [1242, 1243]
    with pytest.raises(SearchError):
        parse_ids("12a", "k")
    text = describe(s("A", catalog_ids=[1242], price_max=250), 0.75)
    assert "kategoria 1242" in text and "≤ 250" in text and "75%" in text

import asyncio

from vinted_bot import load_config


def test_command_tree_builds(tmp_path):
    """Buduje drzewo komend bez łączenia z Discordem."""
    from vinted_bot import SearchCommands, VintedDiscordBot

    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(f"storage:\n  db_path: {tmp_path / 'db.sqlite'}\nlogging:\n  file: null\nsearches: []\n")
    cfg = load_config(cfg_path, env_file=tmp_path / "none.env")

    async def build():
        bot = VintedDiscordBot(cfg, monitor_factory=lambda n: None)
        group = SearchCommands(bot)
        names = {c.name for c in group.commands}
        payload = group.to_dict(bot.tree)
        bot.shutdown()
        return names, payload

    names, payload = asyncio.run(build())
    assert names == {"dodaj", "z-linku", "lista", "usun", "pauza", "wznow"}
    assert payload["default_member_permissions"] == 32  # manage_guild

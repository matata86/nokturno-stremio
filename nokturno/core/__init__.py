"""Sdílené jádro Nokturna — zdroj pravdy pro tři konzumenty.

Doplněk pro Kodi, integrace pro Home Assistant a doplněk pro Stremio sdílejí
tuhle knihovnu. Needituje se u nich, edituje se tady a rozešle se skriptem
`tools/sync_core.py`. Viz README.

Čistý Python bez externích závislostí a bez vazby na hostitele: žádný import
z `xbmc*` ani z `homeassistant`.
"""
from .engine import Engine, NokturnoError, is_sosac_id, split_episode_id

__all__ = ["Engine", "NokturnoError", "is_sosac_id", "split_episode_id"]

"""Nokturno pro Stremio — streamy z WebShare, Sosáče, Sledujteto, FastShare a HellSpy.

Jádro leží v `core/` a je to vysypaná kopie z repa `nokturno-core`; needituje se
tady. Nad ním jsou jen tři vrstvy: `config` sbírá nastavení, `mapping` převádí
streamy do podoby pro Stremio a `routes` obsluhuje endpointy protokolu.
"""
from .routes import VERZE

__all__ = ["VERZE"]

"""Sports-data providers.

A provider turns some upstream service into :class:`scoreboard.models.Game`
objects.  Application code depends on :class:`SportsProvider` only.
"""

from .base import ProviderError, SportsProvider
from .espn import EspnProvider

__all__ = ["ProviderError", "SportsProvider", "EspnProvider", "build_provider"]


def build_provider(name: str, **kwargs) -> SportsProvider:
    """Factory so the provider can be swapped from configuration later."""
    providers = {"espn": EspnProvider}
    key = (name or "espn").strip().lower()
    if key not in providers:
        raise ProviderError(f"Unknown sports provider {name!r}")
    return providers[key](**kwargs)

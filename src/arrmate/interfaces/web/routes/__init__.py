"""Arrmate web routes, split by domain. URL layout is unchanged."""

from . import (
    admin,
    auth,
    command,
    discover,
    downloads,
    library,
    pages,
    plex,
    prowlarr,
    requests,
    settings_web,
    tags,
    transcode,
)
from ._shared import (
    auth_router,
    get_executor,
    get_parser,
    reset_parser,
    router,
    settings,
    templates,
)

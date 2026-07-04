from __future__ import annotations

from .alias_index import default_alias_index
from .catalog import EntityCatalog, default_entity_catalog
from .supported_companies import supported_company_catalog


def warm_entity_catalog_cache() -> EntityCatalog:
    catalog = default_entity_catalog()
    default_alias_index()
    supported_company_catalog()
    return catalog

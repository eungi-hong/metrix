"""The base for every API model.

`json_schema_serialization_defaults_required`: in responses, a field with a
default is always present, so the response schema lists it as required.
Without it, generated clients (frontend/src/api/schema.d.ts) would type every
defaulted list or flag as possibly missing.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ApiModel(BaseModel):
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)

from __future__ import annotations

import json
import sys
from collections.abc import Iterable
from typing import TypeVar

from pydantic import BaseModel

from m365_mcp_scanner.models import ScanDocument

T = TypeVar("T", bound=BaseModel)


def dump_scan_document(doc: ScanDocument) -> str:
    return doc.model_dump_json(indent=2, exclude_none=False)


def dump_model(model: BaseModel) -> str:
    return model.model_dump_json(indent=2, exclude_none=False)


def dump_list(items: Iterable[BaseModel]) -> str:
    return json.dumps(
        [item.model_dump(mode="json") for item in items],
        indent=2,
    )


def write_stdout(payload: str) -> None:
    sys.stdout.write(payload)
    if not payload.endswith("\n"):
        sys.stdout.write("\n")
    sys.stdout.flush()

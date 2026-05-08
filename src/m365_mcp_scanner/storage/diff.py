from __future__ import annotations

from dataclasses import dataclass, field
from typing import TypeVar

from pydantic import BaseModel

from m365_mcp_scanner.models import (
    ConsumptionEdge,
    Finding,
    NormalizedAgent,
    NormalizedMcpServer,
    ScanDocument,
)

T = TypeVar("T", bound=BaseModel)


@dataclass
class SectionDiff[TItem: BaseModel]:
    added: list[TItem] = field(default_factory=list)
    removed: list[TItem] = field(default_factory=list)
    changed: list[tuple[TItem, TItem]] = field(default_factory=list)


@dataclass
class ScanDiff:
    mcp_servers: SectionDiff[NormalizedMcpServer] = field(default_factory=SectionDiff)
    agents: SectionDiff[NormalizedAgent] = field(default_factory=SectionDiff)
    consumption_edges: SectionDiff[ConsumptionEdge] = field(default_factory=SectionDiff)
    findings: SectionDiff[Finding] = field(default_factory=SectionDiff)


def diff_scans(a: ScanDocument, b: ScanDocument) -> ScanDiff:
    return ScanDiff(
        mcp_servers=_diff_keyed(a.mcp_servers, b.mcp_servers, key=lambda s: s.server_id),
        agents=_diff_keyed(a.agents, b.agents, key=lambda x: x.agent_id),
        consumption_edges=_diff_keyed(
            a.consumption_edges,
            b.consumption_edges,
            key=lambda e: f"{e.agent_id}:{e.server_id}",
        ),
        findings=_diff_keyed(a.findings, b.findings, key=lambda f: f.finding_id),
    )


def _diff_keyed[TItem: BaseModel](
    old: list[TItem],
    new: list[TItem],
    *,
    key,  # type: ignore[no-untyped-def]
) -> SectionDiff[TItem]:
    old_map = {key(item): item for item in old}
    new_map = {key(item): item for item in new}
    diff: SectionDiff[TItem] = SectionDiff()
    for k, item in new_map.items():
        if k not in old_map:
            diff.added.append(item)
        elif old_map[k].model_dump() != item.model_dump():
            diff.changed.append((old_map[k], item))
    for k, item in old_map.items():
        if k not in new_map:
            diff.removed.append(item)
    return diff

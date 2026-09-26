"""Production batch registry with one open batch per line and a tonnage book.

A batch moves through three states:

* ``open``    -- the line is producing against it, and it is the only open
                 batch that line may hold;
* ``closed``  -- production has stopped and a tonnage figure is on record, but
                 that tonnage has not yet been booked to the tonnage ledger;
* ``settled`` -- the tonnage was booked, so the batch reconciles against the
                 site shipment figures.

Closing a batch therefore does not finish the paperwork: the closed tonnage
must be settled with :meth:`BatchRegistry.settle`, which writes it into the
tonnage book.  A batch that never reaches ``settled`` shows up as unsettled in
the month end summary.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ..clock import stamp
from ..errors import InvalidRequest, NameConflict, RecordNotFound, StateConflict
from ..store.documents import DocumentStore
from .ledger import OUTCOME_OK, AuditLedger

BATCH_DOC = "line.batches"
LEDGER_DOC = "line.tonnage"
BATCH_OPEN = "open"
BATCH_CLOSED = "closed"
BATCH_SETTLED = "settled"


@dataclass(frozen=True)
class BatchRecord:
    """One production batch, unique across the site."""

    code: str
    unit: str
    kind: str
    state: str
    opened_at: str
    opened_by: str
    closed_at: str = ""
    closed_by: str = ""
    tonnes: float = 0.0
    settled_at: str = ""
    settled_by: str = ""
    settled_tonnes: float = 0.0
    notes: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "unit": self.unit,
            "kind": self.kind,
            "state": self.state,
            "opened_at": self.opened_at,
            "opened_by": self.opened_by,
            "closed_at": self.closed_at,
            "closed_by": self.closed_by,
            "tonnes": self.tonnes,
            "settled_at": self.settled_at,
            "settled_by": self.settled_by,
            "settled_tonnes": self.settled_tonnes,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "BatchRecord":
        return cls(
            code=str(raw.get("code", "")),
            unit=str(raw.get("unit", "")),
            kind=str(raw.get("kind", "")),
            state=str(raw.get("state", BATCH_OPEN)),
            opened_at=str(raw.get("opened_at", "")),
            opened_by=str(raw.get("opened_by", "")),
            closed_at=str(raw.get("closed_at", "")),
            closed_by=str(raw.get("closed_by", "")),
            tonnes=float(raw.get("tonnes", 0.0)),
            settled_at=str(raw.get("settled_at", "")),
            settled_by=str(raw.get("settled_by", "")),
            settled_tonnes=float(raw.get("settled_tonnes", 0.0)),
            notes=str(raw.get("notes", "")),
        )

    def is_open(self) -> bool:
        return self.state == BATCH_OPEN

    def is_closed(self) -> bool:
        return self.state == BATCH_CLOSED

    def is_settled(self) -> bool:
        return self.state == BATCH_SETTLED


@dataclass(frozen=True)
class BatchSettlement:
    """One entry of the tonnage book: a closed batch reconciled to a tonnage."""

    code: str
    unit: str
    settled_tonnes: float
    settled_at: str
    settled_by: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "unit": self.unit,
            "settled_tonnes": self.settled_tonnes,
            "settled_at": self.settled_at,
            "settled_by": self.settled_by,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "BatchSettlement":
        return cls(
            code=str(raw.get("code", "")),
            unit=str(raw.get("unit", "")),
            settled_tonnes=float(raw.get("settled_tonnes", raw.get("tonnes", 0.0))),
            settled_at=str(raw.get("settled_at", "")),
            settled_by=str(raw.get("settled_by", "")),
        )

    def describe(self) -> str:
        return f"batch {self.code} on {self.unit} settled at {self.settled_tonnes} t"


class TonnageBook:
    """The tonnage ledger: every settled batch, booked once, in settlement order.

    This is the month end reconciliation figure: the tonnes here are the
    production the site has finished paperwork for, so it can be compared line
    by line against the dispatch figures.
    """

    def __init__(self, store: DocumentStore, doc_id: str = LEDGER_DOC) -> None:
        self._store = store
        self.doc_id = doc_id

    def entries(self) -> list[BatchSettlement]:
        document = self._store.try_load(self.doc_id)
        if document is None:
            return []
        raw = document.payload.get("entries", [])
        if not isinstance(raw, list):
            return []
        return [BatchSettlement.from_dict(value) for value in raw if isinstance(value, dict)]

    def append(self, settlement: BatchSettlement, moment: datetime) -> list[BatchSettlement]:
        entries = self.entries()
        if any(entry.code == settlement.code for entry in entries):
            raise StateConflict("that batch is already in the tonnage book", code=settlement.code)
        entries.append(settlement)
        self._save(entries, moment)
        return entries

    def codes(self) -> list[str]:
        return [entry.code for entry in self.entries()]

    def for_unit(self, unit: str | None = None) -> list[BatchSettlement]:
        entries = self.entries()
        return entries if unit is None else [entry for entry in entries if entry.unit == unit]

    def total_tonnes(self, unit: str | None = None) -> float:
        return round(sum(entry.settled_tonnes for entry in self.for_unit(unit)), 4)

    def _save(self, entries: list[BatchSettlement], moment: datetime) -> None:
        self._store.save(
            self.doc_id,
            {"entries": [entry.as_dict() for entry in entries]},
            moment,
        )


class BatchRegistry:
    """Opens and closes batches and books their tonnage when they settle.

    A batch code can only ever be used once, and a line can only work against
    one open batch at a time.
    """

    def __init__(self, store: DocumentStore, ledger: AuditLedger) -> None:
        self._store = store
        self._ledger = ledger
        self.doc_id = BATCH_DOC
        self.book = TonnageBook(store)

    def _batches(self) -> dict[str, BatchRecord]:
        document = self._store.try_load(self.doc_id)
        if document is None:
            return {}
        raw = document.payload.get("batches", {})
        if not isinstance(raw, dict):
            return {}
        return {key: BatchRecord.from_dict(value) for key, value in raw.items() if isinstance(value, dict)}

    def _save(self, batches: dict[str, BatchRecord], moment: datetime) -> None:
        self._store.save(
            self.doc_id,
            {"batches": {key: value.as_dict() for key, value in batches.items()}},
            moment,
        )

    def open(
        self,
        code: str,
        unit: str,
        kind: str,
        moment: datetime,
        actor: str,
        *,
        notes: str = "",
    ) -> BatchRecord:
        name = code.strip().upper()
        if not name:
            raise InvalidRequest("a batch needs a code")
        if not unit.strip():
            raise InvalidRequest("a batch needs a line", code=name)
        if not kind.strip():
            raise InvalidRequest("a batch needs a kind", code=name)
        batches = self._batches()
        if name in batches:
            existing = batches[name]
            raise NameConflict(
                "that batch code is already used",
                code=name,
                existing_unit=existing.unit,
                existing_state=existing.state,
            )
        running = self._open_for(batches, unit)
        if running is not None:
            raise StateConflict(
                "that line already has an open batch",
                unit=unit,
                code=running.code,
                opened_at=running.opened_at,
            )
        record = BatchRecord(
            code=name,
            unit=unit,
            kind=kind.strip(),
            state=BATCH_OPEN,
            opened_at=stamp(moment),
            opened_by=actor.strip(),
            notes=notes.strip(),
        )
        batches[name] = record
        self._save(batches, moment)
        self._ledger.record(
            unit,
            "batch.open",
            OUTCOME_OK,
            actor,
            moment,
            subject=name,
            kind=record.kind,
        )
        return record

    def close(
        self,
        code: str,
        moment: datetime,
        actor: str,
        *,
        tonnes: float | None = None,
    ) -> BatchRecord:
        name = code.strip().upper()
        batches = self._batches()
        record = self._require(batches, name)
        if not record.is_open():
            raise StateConflict(
                "that batch is not open",
                code=name,
                state=record.state,
                closed_at=record.closed_at,
            )
        figure = record.tonnes if tonnes is None else float(tonnes)
        closed = BatchRecord(
            **{
                **record.as_dict(),
                "state": BATCH_CLOSED,
                "closed_at": stamp(moment),
                "closed_by": actor.strip(),
                "tonnes": figure,
            }
        )
        batches[name] = closed
        self._save(batches, moment)
        self._ledger.record(
            record.unit,
            "batch.close",
            OUTCOME_OK,
            actor,
            moment,
            subject=name,
            tonnes=closed.tonnes,
        )
        return closed

    def settle(
        self,
        code: str,
        moment: datetime,
        actor: str,
        *,
        tonnes: float | None = None,
    ) -> BatchSettlement:
        """Book a closed batch's tonnage into the ledger and mark it settled."""

        name = code.strip().upper()
        batches = self._batches()
        record = self._require(batches, name)
        if record.is_open():
            raise StateConflict("that batch is still open, close it first", code=name)
        if record.is_settled():
            raise StateConflict("that batch is already settled", code=name, settled_at=record.settled_at)
        figure = record.tonnes if tonnes is None else float(tonnes)
        settlement = BatchSettlement(
            code=name,
            unit=record.unit,
            settled_tonnes=figure,
            settled_at=stamp(moment),
            settled_by=actor.strip(),
        )
        self.book.append(settlement, moment)
        batches[name] = BatchRecord(
            **{
                **record.as_dict(),
                "state": BATCH_SETTLED,
                "tonnes": figure,
                "settled_at": settlement.settled_at,
                "settled_by": settlement.settled_by,
                "settled_tonnes": figure,
            }
        )
        self._save(batches, moment)
        self._ledger.record(
            record.unit,
            "batch.settle",
            OUTCOME_OK,
            actor,
            moment,
            subject=name,
            tonnes=figure,
        )
        return settlement

    def get(self, code: str) -> BatchRecord:
        return self._require(self._batches(), code.strip().upper())

    def open_for(self, unit: str) -> BatchRecord | None:
        """The batch a line is currently producing against, if it has one."""

        return self._open_for(self._batches(), unit)

    def open_batches(self, unit: str | None = None) -> list[BatchRecord]:
        return [
            record
            for record in self._batches().values()
            if record.is_open() and (unit is None or record.unit == unit)
        ]

    def unsettled(self, unit: str | None = None) -> list[BatchRecord]:
        """Closed batches whose tonnage is not yet in the ledger."""

        return [
            record
            for record in self._batches().values()
            if record.is_closed() and (unit is None or record.unit == unit)
        ]

    def settled(self, unit: str | None = None) -> list[BatchRecord]:
        return [
            record
            for record in self._batches().values()
            if record.is_settled() and (unit is None or record.unit == unit)
        ]

    def codes(self) -> list[str]:
        return sorted(self._batches())

    def summary(self) -> dict[str, Any]:
        batches = self._batches()
        open_codes = sorted(record.code for record in batches.values() if record.is_open())
        unsettled = sorted(record.code for record in batches.values() if record.is_closed())
        settled = sorted(record.code for record in batches.values() if record.is_settled())
        return {
            "count": len(batches),
            "open": len(open_codes),
            "open_codes": open_codes,
            "unsettled": unsettled,
            "settled": len(settled),
            "settled_codes": settled,
            "settled_tonnes": self.book.total_tonnes(),
            "tonnage_book": [entry.as_dict() for entry in self.book.entries()],
            "codes": sorted(batches),
        }

    @staticmethod
    def _require(batches: dict[str, BatchRecord], name: str) -> BatchRecord:
        record = batches.get(name)
        if record is None:
            raise RecordNotFound("no such batch", code=name)
        return record

    @staticmethod
    def _open_for(batches: dict[str, BatchRecord], unit: str) -> BatchRecord | None:
        for record in batches.values():
            if record.is_open() and record.unit == unit:
                return record
        return None

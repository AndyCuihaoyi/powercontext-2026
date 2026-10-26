# Copyright (c) 2026 OceanBase.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Row-level CSV capture so a retrieval citation can point back to one record."""

from __future__ import annotations

import codecs
import csv
import io
import json
from dataclasses import dataclass
from typing import Final
from urllib.parse import quote

from pydantic import BaseModel, Field

from powercontext.limits import MAX_SOURCE_ID_LENGTH
from powercontext.sources.models import Source, SourceMaterialization
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY, TextEvidence

CSV_ROW_SOURCE_NAME: Final = "csv-row"
CSV_ROW_SOURCE_VERSION: Final = "1"
CSV_ROW_IDENTITY_PREFIX: Final = "csv"


class CSVDocumentInput(BaseModel):
    """One offline CSV document captured row by row."""

    document_id: str
    primary_key: str
    content: bytes


class CSVRowInput(BaseModel):
    """One captured CSV row, keeping the original column order and values."""

    document_id: str
    primary_key: str
    primary_key_value: str
    columns: tuple[str, ...]
    values: tuple[str, ...]


class CSVRowSource(Source):
    """A durable row-level Source describing exactly one captured record."""

    source_id: str
    document_id: str
    primary_key: str
    primary_key_value: str
    columns: tuple[str, ...]
    values: tuple[str, ...]


def csv_row_source_id(document_id: str, primary_key_value: str) -> str:
    """Build the stable row identity used by every CSV row Source."""

    if not isinstance(document_id, str) or not document_id or document_id != document_id.strip():
        raise ValueError("document_id must be a non-empty string without leading or trailing whitespace")
    if not isinstance(primary_key_value, str) or not primary_key_value:
        raise ValueError("primary_key_value must be a non-empty string")
    source_id = f"{CSV_ROW_IDENTITY_PREFIX}:{quote(document_id, safe='')}:{quote(primary_key_value, safe='')}"
    if len(source_id) > MAX_SOURCE_ID_LENGTH:
        raise ValueError(f"source_id must not exceed {MAX_SOURCE_ID_LENGTH} characters")
    return source_id


def parse_csv_document(value: CSVDocumentInput) -> tuple[CSVRowInput, ...]:
    """Parse one offline CSV document into validated row-level inputs.

    The whole document is parsed and validated before any row is returned, so a
    malformed document never yields a partial result.
    """

    if not isinstance(value, CSVDocumentInput):
        raise ValueError("value must be a CSVDocumentInput")
    document_id = value.document_id
    primary_key = value.primary_key
    if not document_id or document_id != document_id.strip():
        raise ValueError("document_id must be a non-empty string without leading or trailing whitespace")
    if not primary_key:
        raise ValueError("primary_key must be a non-empty string")

    content = value.content
    if content.startswith(codecs.BOM_UTF8):
        content = content[len(codecs.BOM_UTF8) :]
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("content must be valid UTF-8") from error

    try:
        records = list(csv.reader(io.StringIO(text, newline=""), strict=True))
    except csv.Error as error:
        raise ValueError(f"content is not valid CSV: {error}") from error

    header = _read_header(records)
    key_index = header.index(primary_key)

    rows: list[CSVRowInput] = []
    seen_keys: set[str] = set()
    for record in records[1:]:
        if not record:
            continue
        if len(record) != len(header):
            raise ValueError(f"row has {len(record)} fields but the header declares {len(header)}")
        key_value = record[key_index]
        if not key_value or not key_value.strip():
            raise ValueError("primary key value must not be empty or whitespace only")
        if key_value in seen_keys:
            raise ValueError(f"duplicate primary key value: {key_value!r}")
        seen_keys.add(key_value)
        rows.append(
            CSVRowInput(
                document_id=document_id,
                primary_key=primary_key,
                primary_key_value=key_value,
                columns=header,
                values=tuple(record),
            )
        )
    return tuple(rows)


def _read_header(records: list[list[str]]) -> tuple[str, ...]:
    """Validate the header record and return it as an immutable tuple."""

    if not records:
        raise ValueError("content must contain a header record")
    header = tuple(records[0])
    if not header:
        raise ValueError("content must contain a header record")
    for column in header:
        if not column or not column.strip():
            raise ValueError("header fields must not be empty or whitespace only")
    if len(set(header)) != len(header):
        raise ValueError("header fields must be unique")
    return header


class _CSVRowProjection:
    """Expose one captured row as standard textual evidence."""

    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = CSVRowSource
    output_class = TextEvidence

    def project(self, source: CSVRowSource, /) -> TextEvidence:
        row = dict(zip(source.columns, source.values, strict=True))
        return TextEvidence(
            source_type=CSV_ROW_SOURCE_NAME,
            source_id=source.source_id,
            content=json.dumps(row, ensure_ascii=False, separators=(",", ":")),
            metadata={
                "document_id": source.document_id,
                "primary_key": source.primary_key,
                "primary_key_value": source.primary_key_value,
                "columns": list(source.columns),
                "row": row,
            },
        )


@dataclass(frozen=True, slots=True)
class _CSVRowSourceDefinition:
    """Bind the CSV row adapter to one durable version and its projections."""

    name: str = CSV_ROW_SOURCE_NAME
    version: str = CSV_ROW_SOURCE_VERSION
    input_class: type[CSVRowInput] = CSVRowInput
    source_class: type[CSVRowSource] = CSVRowSource
    projections: tuple[_CSVRowProjection, ...] = (_CSVRowProjection(),)

    async def resolve(self, value: CSVRowInput, /) -> CSVRowSource:
        return CSVRowSource(
            name=csv_row_source_id(value.document_id, value.primary_key_value),
            source_id=csv_row_source_id(value.document_id, value.primary_key_value),
            definition_version=self.version,
            materialization=SourceMaterialization.CAPTURED,
            document_id=value.document_id,
            primary_key=value.primary_key,
            primary_key_value=value.primary_key_value,
            columns=value.columns,
            values=value.values,
        )

    async def read(self, source: CSVRowSource, /) -> CSVRowInput:
        return CSVRowInput(
            document_id=source.document_id,
            primary_key=source.primary_key,
            primary_key_value=source.primary_key_value,
            columns=source.columns,
            values=source.values,
        )


CSV_ROW_SOURCE_DEFINITION: _CSVRowSourceDefinition = _CSVRowSourceDefinition()


__all__ = [
    "CSV_ROW_SOURCE_DEFINITION",
    "CSV_ROW_SOURCE_NAME",
    "CSV_ROW_SOURCE_VERSION",
    "CSVDocumentInput",
    "CSVRowInput",
    "CSVRowSource",
    "csv_row_source_id",
    "parse_csv_document",
]

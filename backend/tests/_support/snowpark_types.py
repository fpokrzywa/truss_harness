"""A local stand-in for the Snowpark names the generated Snowflake app's tests use (SP6-5, DECISIONS
B-4000: no Snowflake distribution enters the backend's dependencies): `Row`, the data types, whose
class names `backend_snowflake.py` maps to SQL API `rowType` names, and `StructField`, whose `name`
follows Snowpark 1.55.0: a name arrives quoted (`quoted`) and loses the quotes only when it is a
plain uppercase identifier (`ID`, `A$1`) or `$<digits>`."""

from __future__ import annotations

import re
from dataclasses import dataclass

_UNNECESSARY_QUOTES = re.compile(r'"([_A-Z]+[_A-Z0-9$]*|\$\d+)"')


class Row(tuple[object, ...]):
    """A row of values by position."""

    def __new__(cls, *values: object) -> Row:
        return super().__new__(cls, values)


class DataType:
    """The base of the data types."""


class BooleanType(DataType):
    pass


class DateType(DataType):
    pass


class DoubleType(DataType):
    pass


class LongType(DataType):
    pass


class StringType(DataType):
    pass


@dataclass
class DecimalType(DataType):
    precision: int
    scale: int


def quoted(name: str) -> str:
    """`name` as Snowpark's `quote_name_without_upper_casing` passes it to `StructField`."""
    return '"' + name.replace('"', '""') + '"'


@dataclass
class StructField:
    quoted_name: str
    datatype: DataType

    @property
    def name(self) -> str:
        if _UNNECESSARY_QUOTES.fullmatch(self.quoted_name):
            return self.quoted_name[1:-1]
        return self.quoted_name


@dataclass
class StructType(DataType):
    fields: list[StructField]

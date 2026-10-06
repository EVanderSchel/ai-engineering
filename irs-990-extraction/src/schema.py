"""The extraction schema: a Pydantic model generated from fields.ALL_FIELDS, so the schema, the answer
keys, and the eval can never disagree about field names.

Blank lines: the answer keys use null for a line left blank on the form (different from 0). The obvious
schema, every field "int or null", is rejected by the API: structured outputs compile the schema into a
grammar, and each union type multiplies the cost, so a schema may have at most 16 union-typed fields
(we'd have 42). Instead every field is a plain, required value, and a single list, blank_lines, names
the lines that are blank on the form (their values are placeholders). as_answer() turns those into
None. Claude still has to decide "blank or 0" for every line, it just says so in one place.

The model only declares types: structured outputs enforce types while the answer is generated, but not
rules like "9 digits" (the SDK only copies those into the field description as a hint), so rules live
in checks.py instead.
"""

import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, create_model
from pydantic import Field as PydanticField

from fields import ALL_FIELDS, FIELD_NAMES

PYTHON_TYPES = {"int": int, "str": str, "date": datetime.date}
PLACEHOLDERS = {"int": 0, "str": "", "date": "1900-01-01"}  # value of a blank line's field, ignored

FieldName = Literal[*FIELD_NAMES]

Form990PartI = create_model(
    "Form990PartI",
    __config__=ConfigDict(extra="forbid"),
    __doc__="Form 990 filer identity and Part I (Summary), as printed on page 1.",
    **{
        field.name: (PYTHON_TYPES[field.kind], PydanticField(description=f"{field.line}: {field.description}"))
        for field in ALL_FIELDS
    },
    blank_lines=(
        list[FieldName],
        PydanticField(
            description="Names of the fields whose line or column is left blank on the form. "
            "A line that shows 0 is not blank."
        ),
    ),
)


# Step 6: the same schema plus Claude's own doubts, a list of the fields it isn't sure it read correctly.
# A separate model, so runs without it keep exactly the schema earlier prompt versions were measured on.
Form990PartIWithDoubts = create_model(
    "Form990PartIWithDoubts",
    __base__=Form990PartI,
    unsure_fields=(
        list[FieldName],
        PydanticField(description="Names of the fields whose value you are not sure you read correctly"),
    ),
)


def unsure(extraction: BaseModel) -> list[str]:
    """The fields Claude marked as unsure, or [] for an extraction made without asking."""
    return sorted(set(getattr(extraction, "unsure_fields", [])))


def as_answer(extraction: BaseModel) -> dict:
    """The extraction in answer-key form: plain JSON values, dates as "YYYY-MM-DD", None for blanks."""
    values = extraction.model_dump(mode="json")
    blank = set(values.pop("blank_lines"))
    # The EIN is printed "41-1657792". Asking Claude to drop the dash while copying scrambled the digits
    # next to it (prompt v1), so it's copied as printed and the dash is removed here.
    values["ein"] = values["ein"].replace("-", "").replace(" ", "")
    return {name: None if name in blank else values[name] for name in FIELD_NAMES}


def from_answer(answer: dict) -> BaseModel:
    """The reverse of as_answer: an answer-key dict as the schema object Claude would return."""
    kinds = {field.name: field.kind for field in ALL_FIELDS}
    values = {name: PLACEHOLDERS[kinds[name]] if value is None else value for name, value in answer.items()}
    return Form990PartI(**values, blank_lines=[name for name, value in answer.items() if value is None])

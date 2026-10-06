import datetime

import pytest
from anthropic.lib._parse._transform import transform_schema
from pydantic import ValidationError
from test_checks import gold_answers

from fields import ALL_FIELDS, FIELD_NAMES
from schema import Form990PartI, as_answer, from_answer


def _union_count(schema) -> int:
    """How many properties use anyOf: the API allows at most 16 in one schema."""
    if isinstance(schema, dict):
        return ("anyOf" in schema) + sum(_union_count(v) for v in schema.values())
    if isinstance(schema, list):
        return sum(_union_count(v) for v in schema)
    return 0


def test_schema_needs_every_field_and_has_no_unions():
    schema = transform_schema(Form990PartI)  # what the SDK sends to the API
    assert list(schema["properties"]) == [*FIELD_NAMES, "blank_lines"]
    assert schema["required"] == [*FIELD_NAMES, "blank_lines"]  # Claude must answer every line
    assert schema["additionalProperties"] is False
    assert _union_count(schema) == 0  # the API rejects schemas with more than 16


def test_field_types_follow_fields_py():
    properties = transform_schema(Form990PartI)["properties"]
    expected = {"int": "integer", "str": "string", "date": "string"}
    for field in ALL_FIELDS:
        assert properties[field.name]["type"] == expected[field.kind], field.name
    assert properties["tax_period_end"]["format"] == "date"


def test_blank_lines_can_only_name_real_fields():
    blank_lines = transform_schema(Form990PartI)["properties"]["blank_lines"]
    assert blank_lines["type"] == "array" and blank_lines["items"]["enum"] == FIELD_NAMES
    valid = from_answer(gold_answers()[0]).model_dump()
    Form990PartI.model_validate(valid | {"blank_lines": ["volunteers"]})  # a real field name is fine
    with pytest.raises(ValidationError, match="blank_lines"):
        Form990PartI.model_validate(valid | {"blank_lines": ["line_99"]})


def test_every_answer_key_round_trips_through_the_schema():
    for answer in gold_answers():
        assert as_answer(from_answer(answer)) == answer


def test_blank_lines_become_none_whatever_placeholder_claude_wrote():
    answer = gold_answers()[0]
    extraction = from_answer(answer).model_copy(update={"volunteers": 7, "blank_lines": ["volunteers"]})
    assert as_answer(extraction)["volunteers"] is None


def test_a_listed_zero_is_blank_and_an_unlisted_zero_is_zero():
    extraction = from_answer(gold_answers()[0]).model_copy(
        update={"employees": 0, "volunteers": 0, "blank_lines": ["volunteers"]}
    )
    answer = as_answer(extraction)
    assert (answer["employees"], answer["volunteers"]) == (0, None)


def test_dates_come_back_as_iso_strings_like_the_answer_keys():
    extraction = from_answer(gold_answers()[0]).model_copy(update={"tax_period_begin": datetime.date(2023, 1, 1)})
    assert as_answer(extraction)["tax_period_begin"] == "2023-01-01"


def test_missing_and_extra_fields_are_rejected():
    with pytest.raises(ValidationError):
        Form990PartI.model_validate({"ein": "581524250", "blank_lines": []})
    with pytest.raises(ValidationError):
        Form990PartI.model_validate(from_answer(gold_answers()[0]).model_dump() | {"surprise": 1})


def test_the_ein_dash_is_removed_in_code():
    extraction = from_answer(gold_answers()[0]).model_copy(update={"ein": "41-1657792"})
    assert as_answer(extraction)["ein"] == "411657792"


def test_the_doubts_schema_adds_one_list_and_no_unions():
    from schema import Form990PartIWithDoubts, unsure

    schema = transform_schema(Form990PartIWithDoubts)
    assert list(schema["properties"]) == [*FIELD_NAMES, "blank_lines", "unsure_fields"]
    assert schema["properties"]["unsure_fields"]["items"]["enum"] == FIELD_NAMES
    assert _union_count(schema) == 0
    plain = from_answer(gold_answers()[0])
    doubtful = Form990PartIWithDoubts(**plain.model_dump(), unsure_fields=["volunteers", "ein", "volunteers"])
    assert unsure(doubtful) == ["ein", "volunteers"] and unsure(plain) == []
    assert as_answer(doubtful) == as_answer(plain)  # doubts never leak into the answer

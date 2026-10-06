"""Read answer-key values from an IRS Form 990 e-file XML document."""

import xml.etree.ElementTree as ET

from fields import HEADER_FIELDS, PART_I_FIELDS, Field

NS = {"e": "http://www.irs.gov/efile"}


class NotForm990(ValueError):
    """The XML isn't a Form 990 (for example a 990-EZ or 990-PF, which have different fields)."""


def _path(tag_path: str) -> str:
    """Turn "Filer/EIN" into the namespaced path "e:Filer/e:EIN"."""
    return "/".join(f"e:{part}" for part in tag_path.split("/"))


def _text(element: ET.Element | None) -> str | None:
    """An element's text, including its children's (joined with spaces), or None if it's absent."""
    if element is None:
        return None
    return " ".join(part.strip() for part in element.itertext() if part.strip())


def _convert(field: Field, text: str | None):
    if text is None:
        return None  # element absent: the line was left blank on the form
    if field.kind == "int":
        return int(text)
    return text  # str, and dates as "YYYY-MM-DD"


def answer_key(xml_bytes: bytes) -> dict:
    """Every field in fields.ALL_FIELDS, read from the XML. Raises NotForm990 for other return types."""
    root = ET.fromstring(xml_bytes)
    header = root.find("e:ReturnHeader", NS)
    form = root.find("e:ReturnData/e:IRS990", NS)
    if header is None or form is None:
        raise NotForm990("no ReturnData/IRS990 element: not a Form 990 return")

    values = {}
    for field in HEADER_FIELDS:
        values[field.name] = _convert(field, _text(header.find(_path(field.xml_tag), NS)))
    for field in PART_I_FIELDS:
        values[field.name] = _convert(field, _text(form.find(_path(field.xml_tag), NS)))
    return values

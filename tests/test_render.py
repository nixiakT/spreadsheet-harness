from __future__ import annotations

import json
import shutil
import warnings
import zipfile
from pathlib import Path
from xml.etree import ElementTree

import pytest
from openpyxl import Workbook, load_workbook
from openpyxl.workbook.defined_name import DefinedName
from openpyxl.worksheet.formula import DataTableFormula
from PIL import Image

import spreadsheet_harness.render as render_module
from spreadsheet_harness.errors import RecalculationIntegrityError, RenderError
from spreadsheet_harness.render import (
    PNG_SIGNATURE,
    RenderPage,
    find_libreoffice,
    isolated_user_profile,
    libreoffice_command,
    read_png,
    recalculate_workbook,
    render_workbook,
    sha256_file,
    sheet_inventory_identity,
)


def _save_png(path: Path, *, color: str = "white") -> bytes:
    Image.new("RGB", (8, 6), color=color).save(path, format="PNG")
    return path.read_bytes()


def _save_workbook(path: Path, *, two_sheets: bool = True) -> None:
    workbook = Workbook()
    first = workbook.active
    first.title = "Data"
    first["A1"] = "Name"
    first["B1"] = "Value"
    first.append(["one", 1])
    first.append(["two", 2])
    first["B4"] = "=SUM(B2:B3)"
    if two_sheets:
        second = workbook.create_sheet("Summary")
        second["A1"] = "Total"
        second["B1"] = "=Data!B4"
    workbook.save(path)
    workbook.close()


def test_restore_ooxml_data_tables_recovers_libreoffice_destroyed_region(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.xlsx"
    converted = tmp_path / "converted.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Model"
    sheet["C1"] = 0.1
    sheet["C2"] = 0.2
    sheet["A1"] = DataTableFormula(
        ref="A1:B2",
        dt2D=True,
        r1="C1",
        r2="C2",
    )
    sheet["B1"] = 11.0
    sheet["A2"] = 12.0
    sheet["B2"] = 13.0
    workbook.save(source)
    workbook.close()

    workbook = load_workbook(source, data_only=False)
    sheet = workbook["Model"]
    for coordinate in ("A1", "B1", "A2", "B2"):
        sheet[coordinate] = "=TABLE($C$1,$C$2)"
    workbook.save(converted)
    workbook.close()

    report = render_module._restore_ooxml_data_tables(source, converted)
    workbook = load_workbook(converted, data_only=False)
    sheet = workbook["Model"]
    assert isinstance(sheet["A1"].value, DataTableFormula)
    assert sheet["B1"].value == 11.0
    assert sheet["A2"].value == 12.0
    assert sheet["B2"].value == 13.0
    workbook.close()
    assert report == {"regions": 1, "cells": 4}


def test_restore_ooxml_data_tables_recovers_string_result_cache(
    tmp_path: Path,
) -> None:
    """String-valued tables (e.g. multiple/percentage labels) keep TABLE caches."""
    source = tmp_path / "source-string.xlsx"
    converted = tmp_path / "converted-string.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Model"
    sheet["A1"] = "2.4x/19%"  # result cell referenced by the table anchor
    sheet["A2"] = 0.1
    sheet["B1"] = 0.2
    sheet["B2"] = DataTableFormula(ref="B2:C3", dt2D=True, r1="D1", r2="D2")
    sheet["C2"] = "#REF!"
    sheet["B3"] = "#REF!"
    sheet["C3"] = "#REF!"
    workbook.save(source)
    workbook.close()

    workbook = load_workbook(source, data_only=False)
    sheet = workbook["Model"]
    for coordinate in ("B2", "C2", "B3", "C3"):
        sheet[coordinate] = "=TABLE($D$1,$D$2)"
    workbook.save(converted)
    workbook.close()

    report = render_module._restore_ooxml_data_tables(source, converted)
    values = load_workbook(converted, data_only=True)
    assert values["Model"]["B2"].value == "2.4x/19%"
    assert values["Model"]["C3"].value == "2.4x/19%"
    values.close()
    assert report == {"regions": 1, "cells": 4}


def test_restore_ooxml_data_table_uses_calculated_numeric_anchor(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source-anchor.xlsx"
    converted = tmp_path / "converted-anchor.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Model"
    sheet["A1"] = 0
    sheet["B1"] = 0.1
    sheet["A2"] = 0.2
    sheet["B2"] = DataTableFormula(
        ref="B2:C3",
        dt2D=True,
        r1="E1",
        r2="E2",
    )
    sheet["C2"] = 11.0
    sheet["B3"] = 12.0
    sheet["C3"] = 13.0
    workbook.save(source)
    workbook.close()
    shutil.copy2(source, converted)

    report = render_module._restore_ooxml_data_tables(
        source,
        converted,
        anchor_values={("Model", "B2"): 42.5},
    )

    values = load_workbook(converted, data_only=True)
    assert values["Model"]["B2"].value == 42.5
    values.close()
    formulas = load_workbook(converted, data_only=False)
    assert formulas["Model"]["B2"].value == 42.5
    formulas.close()
    assert report == {"regions": 1, "cells": 4}


def test_restore_ooxml_data_table_keeps_converted_style_for_anchor(
    tmp_path: Path,
) -> None:
    """A compacted LibreOffice style table must not receive source style IDs."""
    source = tmp_path / "source-high-style.xlsx"
    converted = tmp_path / "converted-low-style.xlsx"

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Model"
    sheet["A1"] = 0.1
    sheet["A2"] = 0.2
    sheet["B2"] = DataTableFormula(ref="B2:C3", dt2D=True, r1="A1", r2="A2")
    sheet["C2"] = 11.0
    sheet["B3"] = 12.0
    sheet["C3"] = 13.0
    # Allocate a style ID that a later LibreOffice export can compact away.
    from openpyxl.styles import Font

    for index in range(24):
        sheet.cell(row=10 + index, column=1).font = Font(name=f"SourceFont{index}")
    sheet["B2"].font = Font(name="HighStyleAnchor")
    workbook.save(source)
    workbook.close()

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Model"
    sheet["B2"] = 42.0
    sheet["C2"] = 11.0
    sheet["B3"] = 12.0
    sheet["C3"] = 13.0
    workbook.save(converted)
    workbook.close()

    report = render_module._restore_ooxml_data_tables(
        source,
        converted,
        anchor_values={("Model", "B2"): 42.5},
    )

    output = load_workbook(converted, data_only=False)
    assert output["Model"]["B2"].value == 42.5
    output.close()
    assert report == {"regions": 1, "cells": 4}


def test_restore_ooxml_array_formula_keeps_container_and_converted_body(
    tmp_path: Path,
) -> None:
    from openpyxl.worksheet.formula import ArrayFormula

    source = tmp_path / "source-array.xlsx"
    converted = tmp_path / "converted-array.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Model"
    sheet["A1"] = ArrayFormula(ref="A1:A2", text="=ROW(A1:A2)")
    sheet["A2"] = 2.0
    workbook.save(source)
    workbook.close()

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Model"
    sheet["A1"] = "=ROW(A1:A2)"
    sheet["A2"] = 22.0
    workbook.save(converted)
    workbook.close()

    report = render_module._restore_ooxml_array_formulas(source, converted)

    output = load_workbook(converted, data_only=False)
    assert isinstance(output["Model"]["A1"].value, ArrayFormula)
    assert output["Model"]["A1"].value.text == "=ROW(A1:A2)"
    assert output["Model"]["A2"].value == 22.0
    output.close()
    assert report == {"regions": 1, "cells": 2}


def test_restore_array_formula_kind_keeps_recalculated_cache(tmp_path: Path) -> None:
    from openpyxl.worksheet.formula import ArrayFormula

    source = tmp_path / "source-array-kind.xlsx"
    converted = tmp_path / "converted-array-kind.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Model"
    sheet["B2"] = ArrayFormula(ref="B2:B2", text="=AVERAGE(A1:A3/100)")
    workbook.save(source)
    workbook.close()

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Model"
    sheet["B2"] = "=AVERAGE(A1:A3/100)"
    workbook.save(converted)
    workbook.close()
    with zipfile.ZipFile(converted) as package:
        part = render_module._worksheet_parts_by_name(package)["Model"]
        root = ElementTree.fromstring(package.read(part))
        namespace = root.tag[1:].split("}", 1)[0]
        value = next(
            cell.find(f"{{{namespace}}}v")
            for cell in root.iter(f"{{{namespace}}}c")
            if cell.attrib.get("r") == "B2"
        )
        assert value is not None
        value.text = "0.42"
        xml = ElementTree.tostring(root, encoding="utf-8", xml_declaration=True)
    render_module._replace_ooxml_parts(converted, {part: xml})

    assert render_module._restore_array_formula_kinds(source, converted) == 1
    formula_book = load_workbook(converted, data_only=False)
    assert isinstance(formula_book["Model"]["B2"].value, ArrayFormula)
    formula_book.close()
    value_book = load_workbook(converted, data_only=True)
    assert value_book["Model"]["B2"].value == 0.42
    value_book.close()


def test_restore_ooxml_formula_text_keeps_recalculated_cache(tmp_path: Path) -> None:
    source = tmp_path / "source-formula.xlsx"
    converted = tmp_path / "converted-formula.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Model"
    sheet["A1"] = "=XIRR(A2:A3,B2:B3)"
    workbook.save(source)
    workbook.close()

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Model"
    sheet["A1"] = "=com.sun.star.sheet.addin.Analysis.getXirr(A2:A3,B2:B3)"
    workbook.save(converted)
    workbook.close()

    assert render_module._restore_ooxml_formula_text(source, converted) == 1
    output = load_workbook(converted, data_only=False)
    assert output["Model"]["A1"].value == "=XIRR(A2:A3,B2:B3)"
    output.close()


def test_final_data_table_scenario_preserves_formula_inputs(tmp_path: Path) -> None:
    path = tmp_path / "scenario.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Model"
    sheet["C11"] = "=C20"
    sheet["C20"] = 10.0
    sheet["C31"] = 0.1
    cases = [
        render_module._DataTableAnchorCase(
            sheet="Model",
            anchor="AG18",
            result_cell="AF17",
            row_input="C20",
            column_input="C31",
            row_header_value=0.14,
            column_header_value=12.2,
            final_row_header_value=0.34,
            final_column_header_value=16.2,
        ),
        render_module._DataTableAnchorCase(
            sheet="Model",
            anchor="AG45",
            result_cell="AF44",
            row_input="C11",
            column_input="C20",
            row_header_value=10.2,
            column_header_value=9.8,
            final_row_header_value=18.2,
            final_column_header_value=17.8,
        ),
    ]

    selected = render_module._apply_data_table_final_scenario_inputs(workbook, cases)
    workbook.save(path)
    workbook.close()

    output = load_workbook(path, data_only=False)
    assert output["Model"]["C11"].value == "=C20"
    assert output["Model"]["C20"].value == 18.2
    assert output["Model"]["C31"].value == 0.1
    output.close()
    assert selected == {"Model": {"C11"}}


def test_patch_font_colors_ooxml_preserves_unrelated_package_parts(
    tmp_path: Path,
) -> None:
    from openpyxl.styles import Font

    path = tmp_path / "color-only.xlsx"
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "Model"
    worksheet["A1"] = 10
    worksheet["B2"] = "=A1+1"
    worksheet["B3"] = "=A1+2"
    worksheet["B2"].font = Font(name="Arial", color="FF000000")
    worksheet["B3"].font = Font(name="Arial", color="FF000000")
    workbook.create_sheet("Untouched")["A1"] = "keep"
    workbook.save(path)
    workbook.close()

    with zipfile.ZipFile(path) as package:
        before = {member.filename: package.read(member) for member in package.infolist()}
        model_part = render_module._worksheet_parts_by_name(package)["Model"]

    patched = render_module.patch_font_colors_ooxml(
        path,
        {("Model", "B2"): "00B050", ("Model", "$B$3"): "FF00B050"},
    )

    with zipfile.ZipFile(path) as package:
        after = {member.filename: package.read(member) for member in package.infolist()}
    allowed_changes = {"xl/styles.xml", model_part}
    assert patched == 2
    assert before.keys() == after.keys()
    assert {
        name for name in before if before[name] != after[name]
    } == allowed_changes
    output = load_workbook(path, data_only=False)
    assert output["Model"]["B2"].value == "=A1+1"
    assert output["Model"]["B3"].value == "=A1+2"
    assert output["Model"]["B2"].font.color.rgb == "FF00B050"
    assert output["Model"]["B3"].font.color.rgb == "FF00B050"
    assert output["Untouched"]["A1"].value == "keep"
    output.close()


def test_restore_ooxml_cell_contents_preserves_target_font_style(
    tmp_path: Path,
) -> None:
    from openpyxl.styles import Font

    source = tmp_path / "source.xlsx"
    target = tmp_path / "target.xlsx"
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "Model"
    worksheet["A1"] = 10
    worksheet["B2"] = "=A1+1"
    workbook.save(source)
    workbook.close()
    target.write_bytes(source.read_bytes())
    mutated = load_workbook(target, data_only=False)
    mutated["Model"]["A1"] = 99
    mutated["Model"]["B2"] = "=A1+2"
    mutated["Model"]["B2"].font = Font(color="FFFF0000")
    mutated.save(target)
    mutated.close()

    restored = render_module.restore_ooxml_cell_contents(
        source,
        target,
        minimum_changes=2,
    )

    assert restored == 2
    output = load_workbook(target, data_only=False)
    assert output["Model"]["A1"].value == 10
    assert output["Model"]["B2"].value == "=A1+1"
    assert output["Model"]["B2"].font.color.rgb == "FFFF0000"
    output.close()


def test_restore_ooxml_cell_contents_decodes_package_local_shared_strings(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source-shared-strings.xlsx"
    target = tmp_path / "target-shared-strings.xlsx"
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "Model"
    worksheet["A1"] = "alpha"
    worksheet["A2"] = "beta"
    worksheet["B1"] = "=1+1"
    workbook.save(source)
    workbook.close()
    shutil.copy2(source, target)
    mutated = load_workbook(target, data_only=False)
    mutated["Model"]["B1"] = "=1+2"
    mutated.save(target)
    mutated.close()

    spreadsheet_namespace = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    package_relationship_namespace = (
        "http://schemas.openxmlformats.org/package/2006/relationships"
    )
    content_type_namespace = (
        "http://schemas.openxmlformats.org/package/2006/content-types"
    )

    def use_shared_strings(path: Path, strings: list[str], indices: dict[str, int]) -> None:
        temporary = path.with_name(f".{path.name}.tmp")
        with zipfile.ZipFile(path, "r") as source_package, zipfile.ZipFile(
            temporary, "w"
        ) as target_package:
            replacements: dict[str, bytes] = {}
            sheet = ElementTree.fromstring(source_package.read("xl/worksheets/sheet1.xml"))
            for cell in sheet.iter(f"{{{spreadsheet_namespace}}}c"):
                coordinate = cell.attrib.get("r")
                if coordinate not in indices:
                    continue
                cell.set("t", "s")
                for child in list(cell):
                    cell.remove(child)
                value = ElementTree.SubElement(
                    cell, f"{{{spreadsheet_namespace}}}v"
                )
                value.text = str(indices[coordinate])
            replacements["xl/worksheets/sheet1.xml"] = ElementTree.tostring(
                sheet, encoding="utf-8", xml_declaration=True
            )

            relationships = ElementTree.fromstring(
                source_package.read("xl/_rels/workbook.xml.rels")
            )
            relationship_ids = {item.attrib.get("Id") for item in relationships}
            relationship_id = "rIdSharedStrings"
            assert relationship_id not in relationship_ids
            ElementTree.SubElement(
                relationships,
                f"{{{package_relationship_namespace}}}Relationship",
                {
                    "Id": relationship_id,
                    "Type": (
                        "http://schemas.openxmlformats.org/officeDocument/2006/"
                        "relationships/sharedStrings"
                    ),
                    "Target": "sharedStrings.xml",
                },
            )
            replacements["xl/_rels/workbook.xml.rels"] = ElementTree.tostring(
                relationships, encoding="utf-8", xml_declaration=True
            )

            content_types = ElementTree.fromstring(
                source_package.read("[Content_Types].xml")
            )
            ElementTree.SubElement(
                content_types,
                f"{{{content_type_namespace}}}Override",
                {
                    "PartName": "/xl/sharedStrings.xml",
                    "ContentType": (
                        "application/vnd.openxmlformats-officedocument."
                        "spreadsheetml.sharedStrings+xml"
                    ),
                },
            )
            replacements["[Content_Types].xml"] = ElementTree.tostring(
                content_types, encoding="utf-8", xml_declaration=True
            )

            shared_strings = ElementTree.Element(
                f"{{{spreadsheet_namespace}}}sst",
                {"count": str(len(indices)), "uniqueCount": str(len(strings))},
            )
            for text in strings:
                item = ElementTree.SubElement(
                    shared_strings, f"{{{spreadsheet_namespace}}}si"
                )
                node = ElementTree.SubElement(
                    item, f"{{{spreadsheet_namespace}}}t"
                )
                node.text = text
            replacements["xl/sharedStrings.xml"] = ElementTree.tostring(
                shared_strings, encoding="utf-8", xml_declaration=True
            )

            for info in source_package.infolist():
                target_package.writestr(
                    info,
                    replacements.pop(info.filename, source_package.read(info.filename)),
                )
            for name, payload in replacements.items():
                target_package.writestr(name, payload)
        temporary.replace(path)

    use_shared_strings(source, ["alpha", "beta"], {"A1": 0, "A2": 1})
    use_shared_strings(
        target,
        ["inserted", "alpha", "changed"],
        {"A1": 1, "A2": 2},
    )

    restored = render_module.restore_ooxml_cell_contents(
        source,
        target,
        selected_coordinates={"Model": ["A1", "A2", "B1"]},
    )

    assert restored == 2
    output = load_workbook(target, data_only=False)
    assert output["Model"]["A1"].value == "alpha"
    assert output["Model"]["A2"].value == "beta"
    assert output["Model"]["B1"].value == "=1+1"
    output.close()


def test_transplant_formula_caches_preserves_formula_and_font_style(
    tmp_path: Path,
) -> None:
    from openpyxl.styles import Font

    target = tmp_path / "target.xlsx"
    recalculated = tmp_path / "recalculated.xlsx"
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "Model"
    worksheet["A1"] = 2
    worksheet["B1"] = "=+A1*3"
    worksheet["B1"].font = Font(color="FFFF0000")
    workbook.save(target)
    workbook.close()
    recalculated.write_bytes(target.read_bytes())
    with zipfile.ZipFile(recalculated) as package:
        part = render_module._worksheet_parts_by_name(package)["Model"]
        root = render_module._parse_inventory_xml(
            package.read(part),
            label="test recalculated worksheet",
        )
    namespace = render_module._xml_namespace(root.tag)
    cell_tag = f"{{{namespace}}}c"
    value_tag = f"{{{namespace}}}v"
    formula_tag = f"{{{namespace}}}f"
    formula_cell = next(
        cell for cell in root.iter(cell_tag) if cell.attrib.get("r") == "B1"
    )
    value = formula_cell.find(value_tag)
    assert value is not None
    formula = formula_cell.find(formula_tag)
    assert formula is not None
    formula.text = "A1*3"
    value.text = "6"
    ElementTree.register_namespace("", namespace)
    render_module._replace_ooxml_parts(
        recalculated,
        {part: ElementTree.tostring(root, encoding="utf-8", xml_declaration=True)},
    )

    transplanted = render_module.transplant_ooxml_formula_cached_values(
        recalculated,
        target,
    )

    assert transplanted == 1
    formula_workbook = load_workbook(target, data_only=False)
    assert formula_workbook["Model"]["B1"].value == "=+A1*3"
    assert formula_workbook["Model"]["B1"].font.color.rgb == "FFFF0000"
    formula_workbook.close()
    value_workbook = load_workbook(target, data_only=True)
    assert value_workbook["Model"]["B1"].value == 6
    value_workbook.close()


def test_transplant_formula_caches_can_include_data_table_body(
    tmp_path: Path,
) -> None:
    target = tmp_path / "data-table-target.xlsx"
    recalculated = tmp_path / "data-table-recalculated.xlsx"
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "Model"
    worksheet["A1"] = DataTableFormula(ref="A1:B2", dt2D=True, r1="C1", r2="C2")
    worksheet["B1"] = 11
    worksheet["A2"] = 12
    worksheet["B2"] = 13
    workbook.save(target)
    workbook.close()
    recalculated.write_bytes(target.read_bytes())

    with zipfile.ZipFile(recalculated) as package:
        part = render_module._worksheet_parts_by_name(package)["Model"]
        root = render_module._parse_inventory_xml(
            package.read(part), label="test recalculated data table"
        )
    namespace = render_module._xml_namespace(root.tag)
    cell_tag = f"{{{namespace}}}c"
    value_tag = f"{{{namespace}}}v"
    replacements = {"A1": "21", "B1": "22", "A2": "23", "B2": "24"}
    for cell in root.iter(cell_tag):
        if cell.attrib.get("r") not in replacements:
            continue
        value = cell.find(value_tag)
        if value is None:
            value = ElementTree.SubElement(cell, value_tag)
        value.text = replacements[str(cell.attrib["r"])]
        cell.attrib.pop("t", None)
    ElementTree.register_namespace("", namespace)
    render_module._replace_ooxml_parts(
        recalculated,
        {part: ElementTree.tostring(root, encoding="utf-8", xml_declaration=True)},
    )

    transplanted = render_module.transplant_ooxml_formula_cached_values(
        recalculated,
        target,
        include_data_table_regions=True,
    )

    assert transplanted == 4
    formulas = load_workbook(target, data_only=False)
    assert isinstance(formulas["Model"]["A1"].value, DataTableFormula)
    formulas.close()
    values = load_workbook(target, data_only=True)
    assert [values["Model"][cell].value for cell in ("A1", "B1", "A2", "B2")] == [
        21,
        22,
        23,
        24,
    ]
    values.close()


def _minimal_workbook_xml(
    sheets: str,
    *,
    namespace: str = "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    relationship_namespace: str = (
        "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    ),
) -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<workbook xmlns="{namespace}" xmlns:r="{relationship_namespace}">'
        f"<sheets>{sheets}</sheets>"
        "</workbook>"
    ).encode()


def _minimal_relationships_xml(*, strict: bool = False) -> bytes:
    type_prefix = (
        "http://purl.oclc.org/ooxml/officeDocument/relationships/"
        if strict
        else "http://schemas.openxmlformats.org/officeDocument/2006/relationships/"
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        f'<Relationship Id="rId1" Type="{type_prefix}worksheet" '
        'Target="worksheets/sheet1.xml"/>'
        f'<Relationship Id="rId2" Type="{type_prefix}chartsheet" '
        'Target="chartsheets/sheet1.xml"/>'
        f'<Relationship Id="rel-data" Type="{type_prefix}worksheet" '
        'Target="worksheets/data.xml"/>'
        f'<Relationship Id="rel-chart" Type="{type_prefix}chartsheet" '
        'Target="chartsheets/chart.xml"/>'
        '</Relationships>'
    ).encode()


def _write_minimal_ooxml(
    path: Path,
    workbook_xml: bytes | None,
    *,
    relationships_xml: bytes | None = None,
) -> None:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as package:
        package.writestr("[Content_Types].xml", "<Types/>")
        if workbook_xml is not None:
            package.writestr("xl/workbook.xml", workbook_xml)
        package.writestr(
            "xl/_rels/workbook.xml.rels",
            relationships_xml
            if relationships_xml is not None
            else _minimal_relationships_xml(),
        )


def _replace_zip_part(path: Path, part_name: str, replacement: bytes) -> None:
    rewritten = path.with_name(f"{path.stem}-rewritten{path.suffix}")
    with zipfile.ZipFile(path) as source:
        parts = [
            (member.filename, replacement if member.filename == part_name else source.read(member))
            for member in source.infolist()
        ]
    with zipfile.ZipFile(rewritten, "w", compression=zipfile.ZIP_DEFLATED) as target:
        for name, content in parts:
            target.writestr(name, content)
    rewritten.replace(path)


def test_isolated_user_profile_is_unique_and_removed() -> None:
    paths: list[Path] = []
    uris: list[str] = []
    for _ in range(2):
        with isolated_user_profile() as (profile, uri):
            paths.append(profile)
            uris.append(uri)
            assert profile.is_dir()
            assert uri == profile.as_uri()
            assert uri.startswith("file://")
        assert not profile.exists()

    assert paths[0] != paths[1]
    assert uris[0] != uris[1]


def test_iterative_profile_and_workbook_flag_detection(tmp_path: Path) -> None:
    workbook_path = tmp_path / "circular.xlsx"
    workbook = Workbook()
    workbook.active.title = "Model"
    workbook.active["A1"] = 1
    workbook.defined_names.add(DefinedName("circ", attr_text="Model!$A$1"))
    workbook.calculation.iterate = True
    workbook.save(workbook_path)
    workbook.close()

    assert render_module._requires_iterative_calculation(workbook_path)
    with isolated_user_profile(iterative_calculation=True) as (profile, _):
        registry = profile / "user" / "registrymodifications.xcu"
        assert registry.is_file()
        contents = registry.read_text(encoding="utf-8")
        assert "IterativeReference" in contents
        assert "<value>true</value>" in contents


def test_libreoffice_command_contains_private_profile(tmp_path: Path) -> None:
    source = tmp_path / "source.xlsx"
    source.touch()
    output = tmp_path / "output"
    profile_uri = (tmp_path / "profile with spaces").resolve().as_uri()

    command = libreoffice_command("soffice", source, output, "pdf", profile_uri)

    assert command[0] == "soffice"
    assert "--headless" in command
    assert f"-env:UserInstallation={profile_uri}" in command
    assert command[-1] == str(source)
    assert command[command.index("--outdir") + 1] == str(output)


def test_read_png_returns_original_bytes(tmp_path: Path) -> None:
    png = tmp_path / "original.png"
    original = _save_png(png, color="navy")

    loaded = read_png(png)

    assert loaded == original
    assert loaded.startswith(PNG_SIGNATURE)


def test_read_png_rejects_other_files(tmp_path: Path) -> None:
    text = tmp_path / "not-an-image.png"
    text.write_text("not png", encoding="utf-8")

    with pytest.raises(RenderError, match="Not a PNG"):
        read_png(text)


def test_sheet_inventory_identity_includes_chart_sheets_without_openpyxl_loader(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "charts.xlsx"
    workbook = Workbook()
    workbook.active.title = "Data"
    chart_sheet = workbook.create_chartsheet("Chart")
    chart_sheet.sheet_state = "hidden"
    workbook.save(source)
    workbook.close()

    def forbidden_loader(*args: object, **kwargs: object) -> object:
        raise AssertionError("sheet inventory must not use the openpyxl workbook loader")

    monkeypatch.setattr(render_module, "load_workbook", forbidden_loader)
    identity = sheet_inventory_identity(source)

    assert identity["sheets"] == [
        {"index": 0, "kind": "worksheet", "name": "Data", "visibility": "visible"},
        {"index": 1, "kind": "chartsheet", "name": "Chart", "visibility": "hidden"},
    ]


def test_sheet_inventory_accepts_strict_namespace_and_defaults_visible(
    tmp_path: Path,
) -> None:
    source = tmp_path / "strict.xlsx"
    xml = _minimal_workbook_xml(
        (
            '<sheet name="Data" sheetId="7" r:id="rel-data"/>'
            '<sheet name="Chart" sheetId="11" state="veryHidden" r:id="rel-chart"/>'
        ),
        namespace="http://purl.oclc.org/ooxml/spreadsheetml/main",
        relationship_namespace="http://purl.oclc.org/ooxml/officeDocument/relationships",
    )
    _write_minimal_ooxml(
        source,
        xml,
        relationships_xml=_minimal_relationships_xml(strict=True),
    )

    identity = sheet_inventory_identity(source)

    assert identity["sheets"] == [
        {"index": 0, "kind": "worksheet", "name": "Data", "visibility": "visible"},
        {
            "index": 1,
            "kind": "chartsheet",
            "name": "Chart",
            "visibility": "veryHidden",
        },
    ]


@pytest.mark.parametrize(
    ("workbook_xml", "message"),
    [
        (b"<workbook", "malformed"),
        (
            _minimal_workbook_xml(
                '<sheet name="Data" sheetId="1" r:id="rId1"/>',
                namespace="urn:not-spreadsheetml",
            ),
            "unsupported SpreadsheetML namespace",
        ),
        (
            _minimal_workbook_xml(
                '<sheet name="Data" sheetId="1" r:id="rId1"/>'
                '<sheet name="data" sheetId="2" r:id="rId2"/>'
            ),
            "duplicate sheet names",
        ),
        (
            _minimal_workbook_xml(
                '<sheet name="Data" sheetId="1" r:id="rId1"/>'
                '<sheet name="Chart" sheetId="1" r:id="rId2"/>'
            ),
            "duplicate sheetId",
        ),
        (
            _minimal_workbook_xml(
                '<sheet name="Data" sheetId="1" r:id="rId1"/>'
                '<sheet name="Chart" sheetId="2" r:id="rId1"/>'
            ),
            "duplicate sheet relationships",
        ),
        (
            _minimal_workbook_xml(
                '<sheet name="Data" sheetId="1" state="sometimes" r:id="rId1"/>'
            ),
            "invalid visibility",
        ),
        (
            _minimal_workbook_xml('<sheet name="Data" sheetId="1"/>'),
            "missing relationship identifier",
        ),
        (
            _minimal_workbook_xml(
                '<sheet name="Data" sheetId="1" r:id="rId1"/>',
                relationship_namespace="urn:not-office-relationships",
            ),
            "missing relationship identifier",
        ),
    ],
)
def test_sheet_inventory_rejects_malformed_namespace_and_duplicate_records(
    tmp_path: Path,
    workbook_xml: bytes,
    message: str,
) -> None:
    source = tmp_path / "invalid.xlsx"
    _write_minimal_ooxml(source, workbook_xml)

    with pytest.raises(RenderError, match=message):
        sheet_inventory_identity(source)


def test_sheet_inventory_rejects_missing_and_duplicate_workbook_parts(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "missing.xlsx"
    _write_minimal_ooxml(missing, None)
    with pytest.raises(RenderError, match="exactly one xl/workbook.xml; found 0"):
        sheet_inventory_identity(missing)

    duplicate = tmp_path / "duplicate.xlsx"
    xml = _minimal_workbook_xml('<sheet name="Data" sheetId="1" r:id="rId1"/>')
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        with zipfile.ZipFile(duplicate, "w") as package:
            package.writestr("xl/workbook.xml", xml)
            package.writestr("xl/workbook.xml", xml)
    with pytest.raises(RenderError, match="exactly one xl/workbook.xml; found 2"):
        sheet_inventory_identity(duplicate)


def test_sheet_inventory_rejects_missing_duplicate_and_malformed_relationship_parts(
    tmp_path: Path,
) -> None:
    xml = _minimal_workbook_xml('<sheet name="Data" sheetId="1" r:id="rId1"/>')
    missing = tmp_path / "missing-rels.xlsx"
    with zipfile.ZipFile(missing, "w") as package:
        package.writestr("xl/workbook.xml", xml)
    with pytest.raises(RenderError, match="exactly one xl/_rels/workbook.xml.rels; found 0"):
        sheet_inventory_identity(missing)

    duplicate = tmp_path / "duplicate-rels.xlsx"
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        with zipfile.ZipFile(duplicate, "w") as package:
            package.writestr("xl/workbook.xml", xml)
            package.writestr("xl/_rels/workbook.xml.rels", _minimal_relationships_xml())
            package.writestr("xl/_rels/workbook.xml.rels", _minimal_relationships_xml())
    with pytest.raises(RenderError, match="exactly one xl/_rels/workbook.xml.rels; found 2"):
        sheet_inventory_identity(duplicate)

    malformed = tmp_path / "malformed-rels.xlsx"
    _write_minimal_ooxml(malformed, xml, relationships_xml=b"<Relationships")
    with pytest.raises(RenderError, match="workbook relationships XML is malformed"):
        sheet_inventory_identity(malformed)


@pytest.mark.parametrize(
    ("relationships", "message"),
    [
        (
            '<Relationships xmlns="urn:not-package-relationships">'
            '<Relationship Id="rId1" Type="urn:worksheet" Target="sheet.xml"/>'
            '</Relationships>',
            "unsupported namespace",
        ),
        (
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="other" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
            'Target="worksheets/sheet1.xml"/>'
            '</Relationships>',
            "missing workbook relationship",
        ),
        (
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
            'Target="https://example.test/sheet.xml" TargetMode="External"/>'
            '</Relationships>',
            "must target an internal",
        ),
        (
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" '
            'Target="styles.xml"/>'
            '</Relationships>',
            "unsupported sheet type",
        ),
    ],
)
def test_sheet_inventory_rejects_invalid_relationship_resolution(
    tmp_path: Path,
    relationships: str,
    message: str,
) -> None:
    source = tmp_path / "invalid-relationship.xlsx"
    xml = _minimal_workbook_xml('<sheet name="Data" sheetId="1" r:id="rId1"/>')
    _write_minimal_ooxml(source, xml, relationships_xml=relationships.encode())

    with pytest.raises(RenderError, match=message):
        sheet_inventory_identity(source)


def test_sheet_inventory_rejects_dtd_entities_and_damaged_zip(tmp_path: Path) -> None:
    malicious = tmp_path / "entity.xlsx"
    xml = _minimal_workbook_xml(
        '<sheet name="&payload;" sheetId="1" r:id="rId1"/>'
    ).replace(
        b'<workbook xmlns=',
        b'<!DOCTYPE workbook [<!ENTITY payload "expanded">]><workbook xmlns=',
        1,
    )
    _write_minimal_ooxml(malicious, xml)
    with pytest.raises(RenderError, match="DTD or entity"):
        sheet_inventory_identity(malicious)

    damaged = tmp_path / "damaged.xlsx"
    damaged.write_bytes(b"PK\x03\x04truncated")
    with pytest.raises(RenderError, match="Could not read OOXML workbook package"):
        sheet_inventory_identity(damaged)


def test_sheet_inventory_rejects_wrong_namespace_sheet_container(tmp_path: Path) -> None:
    source = tmp_path / "mixed-namespace.xlsx"
    xml = (
        b'<?xml version="1.0" encoding="UTF-8"?>'
        b'<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        b'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
        b'xmlns:evil="urn:evil">'
        b'<evil:sheets><evil:sheet name="Data" sheetId="1" r:id="rId1"/></evil:sheets>'
        b'</workbook>'
    )
    _write_minimal_ooxml(source, xml)

    with pytest.raises(RenderError, match="exactly one sheets element"):
        sheet_inventory_identity(source)


def test_sheet_inventory_rejects_missing_or_repeated_sheets_elements(tmp_path: Path) -> None:
    namespace = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    relationship_namespace = (
        "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    )
    missing = tmp_path / "missing-sheets.xlsx"
    _write_minimal_ooxml(
        missing,
        (
            f'<workbook xmlns="{namespace}" xmlns:r="{relationship_namespace}"/>'
        ).encode(),
    )
    with pytest.raises(RenderError, match="exactly one sheets element; found 0"):
        sheet_inventory_identity(missing)

    repeated = tmp_path / "repeated-sheets.xlsx"
    _write_minimal_ooxml(
        repeated,
        (
            f'<workbook xmlns="{namespace}" xmlns:r="{relationship_namespace}">'
            '<sheets><sheet name="Data" sheetId="1" r:id="rId1"/></sheets>'
            '<sheets><sheet name="Chart" sheetId="2" r:id="rId2"/></sheets>'
            '</workbook>'
        ).encode(),
    )
    with pytest.raises(RenderError, match="exactly one sheets element; found 2"):
        sheet_inventory_identity(repeated)


def test_render_page_dict_has_view_image_fields(tmp_path: Path) -> None:
    png = tmp_path / "page.png"
    _save_png(png)
    page = RenderPage(
        index=3,
        path=png,
        sha256=sha256_file(png),
        width=8,
        height=6,
        sheet="Data",
        sheet_page=2,
    )

    payload = page.to_dict(relative_to=tmp_path)

    assert payload["image_path"] == str(png.resolve())
    assert payload["path"] == "page.png"
    assert payload["index"] == 3
    assert payload["page"] == 2
    assert payload["sheet"] == "Data"
    assert payload["sheet_page"] == 2
    assert payload["width"] == 8
    assert payload["height"] == 6
    assert len(payload["sha256"]) == 64


def test_single_sheet_copy_hides_only_disposable_copy(tmp_path: Path) -> None:
    source = tmp_path / "book.xlsx"
    target = tmp_path / "single.xlsx"
    _save_workbook(source)
    before = source.read_bytes()

    render_module._make_single_sheet_copy(source, target, "Summary")

    copied = load_workbook(target)
    try:
        assert copied["Data"].sheet_state == "hidden"
        assert copied["Summary"].sheet_state == "visible"
        assert copied.active.title == "Summary"
    finally:
        copied.close()
    assert source.read_bytes() == before


def test_pymupdf_rasterization_produces_unmodified_png(tmp_path: Path) -> None:
    module = render_module._pymupdf_module()
    pdf = tmp_path / "source.pdf"
    document = module.open()
    page = document.new_page(width=200, height=100)
    page.insert_text((20, 50), "Spreadsheet")
    document.save(str(pdf))
    document.close()

    pages = render_module._rasterize_pdf(
        pdf,
        tmp_path / "png",
        dpi=72,
        filename_prefix="page",
        sheet="Data",
    )

    assert len(pages) == 1
    assert read_png(pages[0].path).startswith(PNG_SIGNATURE)
    assert pages[0].width == 200
    assert pages[0].height == 100
    assert pages[0].sheet == "Data"
    assert pages[0].sheet_page == 1


def test_per_sheet_failure_falls_back_and_records_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "book.xlsx"
    _save_workbook(source)
    before = source.read_bytes()

    monkeypatch.setattr(render_module, "find_libreoffice", lambda explicit=None: "/fake/soffice")
    monkeypatch.setattr(render_module, "libreoffice_version", lambda binary: "LibreOffice test")
    monkeypatch.setattr(render_module, "pymupdf_version", lambda: "PyMuPDF test")

    def fail_per_sheet(*args: object, **kwargs: object) -> list[object]:
        raise RenderError("deliberate per-sheet failure")

    def fake_whole(
        source_copy: Path,
        work_dir: Path,
        **kwargs: object,
    ) -> list[render_module._RasterizedPage]:
        assert source_copy != source
        png = work_dir / "png" / "workbook-page-0001.png"
        png.parent.mkdir(parents=True)
        _save_png(png)
        return [
            render_module._RasterizedPage(
                path=png,
                width=8,
                height=6,
                sheet=None,
                sheet_page=None,
            )
        ]

    monkeypatch.setattr(render_module, "_render_per_sheet", fail_per_sheet)
    monkeypatch.setattr(render_module, "_render_whole_workbook", fake_whole)

    result = render_workbook(source, tmp_path / "rendered")
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))

    assert source.read_bytes() == before
    assert result.mode == "whole_workbook"
    assert manifest["backend"] == "libreoffice-headless+pymupdf"
    assert manifest["version"]["libreoffice"] == "LibreOffice test"
    assert manifest["hash"] == sha256_file(source)
    assert manifest["page_count"] == 1
    assert manifest["fallback"]["from"] == "per_sheet"
    assert "deliberate per-sheet failure" in manifest["fallback"]["reason"]
    page_payload = manifest["pages"][0]
    assert Path(page_payload["image_path"]).is_absolute()
    assert page_payload["sheet"] is None
    assert page_payload["page"] == 1
    assert read_png(page_payload["image_path"]).startswith(PNG_SIGNATURE)


def test_recalculation_uses_private_copy_and_atomic_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "working.xlsx"
    _save_workbook(source)
    original_hash = sha256_file(source)
    seen_sources: list[Path] = []

    monkeypatch.setattr(render_module, "find_libreoffice", lambda explicit=None: "/fake/soffice")
    monkeypatch.setattr(render_module, "libreoffice_version", lambda binary: "LibreOffice test")

    def fake_convert(
        source_copy: Path,
        output_dir: Path,
        **kwargs: object,
    ) -> Path:
        seen_sources.append(source_copy.resolve())
        assert source_copy.resolve() != source.resolve()
        output_dir.mkdir(parents=True)
        converted = output_dir / "working.xlsx"
        converted.write_bytes(source_copy.read_bytes())
        return converted

    monkeypatch.setattr(render_module, "_convert_with_libreoffice", fake_convert)

    metadata = recalculate_workbook(source, source)

    assert seen_sources and seen_sources[0] != source.resolve()
    assert sha256_file(source) == metadata["output_sha256"]
    assert metadata["backend"] == "libreoffice-headless"
    assert metadata["calculation_mode"] == "uno-calculate-all"
    assert metadata["iterative_calculation"] == {
        "enabled": True,
        "steps": 100,
        "minimum_change": 0.0001,
        "source_requested": False,
    }
    assert metadata["version"] == "LibreOffice test"
    assert metadata["source_sha256"] == original_hash
    assert metadata["destination_path"] == str(source.resolve())
    assert metadata["atomic_replace"] is True
    assert metadata["published"] is True
    integrity = metadata["sheet_inventory_integrity"]
    assert integrity["matched"] is True
    assert integrity["pre"]["sheets"] == integrity["post"]["sheets"]
    assert integrity["pre"]["inventory_sha256"] == integrity["post"]["inventory_sha256"]


def test_recalculation_validates_chartsheet_package_without_openpyxl_loader(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "charts.xlsx"
    workbook = Workbook()
    workbook.active.title = "Data"
    workbook.create_chartsheet("Chart").sheet_state = "hidden"
    workbook.save(source)
    workbook.close()

    monkeypatch.setattr(render_module, "find_libreoffice", lambda explicit=None: "/fake/soffice")
    monkeypatch.setattr(render_module, "libreoffice_version", lambda binary: "LibreOffice test")

    def fake_convert(source_copy: Path, output_dir: Path, **kwargs: object) -> Path:
        output_dir.mkdir(parents=True)
        converted = output_dir / source.name
        converted.write_bytes(source_copy.read_bytes())
        return converted

    def forbidden_loader(*args: object, **kwargs: object) -> object:
        raise AssertionError("recalculation validation must not load chartsheets with openpyxl")

    monkeypatch.setattr(render_module, "_convert_with_libreoffice", fake_convert)
    monkeypatch.setattr(render_module, "load_workbook", forbidden_loader)

    metadata = recalculate_workbook(source, source)

    assert metadata["sheet_inventory_integrity"]["matched"] is True
    assert metadata["sheet_inventory_integrity"]["pre"]["sheets"] == [
        {"index": 0, "kind": "worksheet", "name": "Data", "visibility": "visible"},
        {"index": 1, "kind": "chartsheet", "name": "Chart", "visibility": "hidden"},
    ]


def test_recalculation_detects_chartsheet_to_worksheet_kind_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "charts.xlsx"
    workbook = Workbook()
    workbook.active.title = "Data"
    workbook.create_chartsheet("Chart").sheet_state = "hidden"
    workbook.save(source)
    workbook.close()
    before = source.read_bytes()

    monkeypatch.setattr(render_module, "find_libreoffice", lambda explicit=None: "/fake/soffice")
    monkeypatch.setattr(render_module, "libreoffice_version", lambda binary: "LibreOffice test")

    def fake_convert(source_copy: Path, output_dir: Path, **kwargs: object) -> Path:
        output_dir.mkdir(parents=True)
        converted = output_dir / source.name
        converted.write_bytes(source_copy.read_bytes())
        with zipfile.ZipFile(converted) as package:
            relationships = package.read("xl/_rels/workbook.xml.rels")
        relationships = relationships.replace(b"/chartsheet", b"/worksheet")
        _replace_zip_part(converted, "xl/_rels/workbook.xml.rels", relationships)
        return converted

    monkeypatch.setattr(render_module, "_convert_with_libreoffice", fake_convert)

    with pytest.raises(RecalculationIntegrityError) as caught:
        recalculate_workbook(source, source)

    integrity = caught.value.evidence["sheet_inventory_integrity"]
    assert source.read_bytes() == before
    assert integrity["matched"] is False
    assert [sheet["name"] for sheet in integrity["pre"]["sheets"]] == ["Data", "Chart"]
    assert [sheet["name"] for sheet in integrity["post"]["sheets"]] == ["Data", "Chart"]
    assert [sheet["kind"] for sheet in integrity["pre"]["sheets"]] == [
        "worksheet",
        "chartsheet",
    ]
    assert [sheet["kind"] for sheet in integrity["post"]["sheets"]] == [
        "worksheet",
        "worksheet",
    ]


@pytest.mark.parametrize("suffix", [".ods", ".xls", ".csv"])
def test_recalculation_preserves_non_ooxml_format_support(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    suffix: str,
) -> None:
    source = tmp_path / f"working{suffix}"
    source.write_bytes(b"non-ooxml-source")

    monkeypatch.setattr(render_module, "find_libreoffice", lambda explicit=None: "/fake/soffice")
    monkeypatch.setattr(render_module, "libreoffice_version", lambda binary: "LibreOffice test")

    def fake_convert(source_copy: Path, output_dir: Path, **kwargs: object) -> Path:
        output_dir.mkdir(parents=True)
        converted = output_dir / source.name
        converted.write_bytes(b"recalculated-non-ooxml")
        return converted

    monkeypatch.setattr(render_module, "_convert_with_libreoffice", fake_convert)

    metadata = recalculate_workbook(source, source)

    assert source.read_bytes() == b"recalculated-non-ooxml"
    assert metadata["published"] is True
    assert metadata["sheet_inventory_integrity"] == {
        "schema_version": 2,
        "policy": "exact-ordered-sheet-kind-name-visibility-v2",
        "enforced": False,
        "matched": None,
        "pre": None,
        "post": None,
        "reason": "source-or-destination-is-not-ooxml",
    }


@pytest.mark.parametrize("mutation", ["rename", "reorder", "visibility"])
def test_recalculation_fails_closed_when_sheet_identity_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    source = tmp_path / "working.xlsx"
    _save_workbook(source)
    before = source.read_bytes()
    before_identity = sheet_inventory_identity(source)

    monkeypatch.setattr(render_module, "find_libreoffice", lambda explicit=None: "/fake/soffice")
    monkeypatch.setattr(render_module, "libreoffice_version", lambda binary: "LibreOffice test")

    def fake_convert(
        source_copy: Path,
        output_dir: Path,
        **kwargs: object,
    ) -> Path:
        output_dir.mkdir(parents=True)
        converted = output_dir / "working.xlsx"
        workbook = load_workbook(source_copy)
        try:
            if mutation == "rename":
                workbook["Summary"].title = "Changed"
            elif mutation == "reorder":
                workbook.move_sheet(workbook["Summary"], offset=-1)
            else:
                workbook["Summary"].sheet_state = "hidden"
            workbook.save(converted)
        finally:
            workbook.close()
        return converted

    monkeypatch.setattr(render_module, "_convert_with_libreoffice", fake_convert)

    with pytest.raises(RecalculationIntegrityError) as caught:
        recalculate_workbook(source, source)

    evidence = caught.value.evidence
    integrity = evidence["sheet_inventory_integrity"]
    failure_artifact = Path(evidence["failure_artifact_path"])
    assert source.read_bytes() == before
    assert evidence["atomic_replace"] is False
    assert evidence["published"] is False
    assert integrity["matched"] is False
    assert integrity["pre"] == before_identity
    assert integrity["post"]["sheets"] != before_identity["sheets"]
    assert failure_artifact.is_file()
    assert sha256_file(failure_artifact) == evidence["output_sha256"]
    assert sheet_inventory_identity(failure_artifact) == integrity["post"]


def test_recalculation_identity_failure_survives_evidence_publish_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "working.xlsx"
    _save_workbook(source)
    before = source.read_bytes()

    monkeypatch.setattr(render_module, "find_libreoffice", lambda explicit=None: "/fake/soffice")
    monkeypatch.setattr(render_module, "libreoffice_version", lambda binary: "LibreOffice test")

    def fake_convert(source_copy: Path, output_dir: Path, **kwargs: object) -> Path:
        output_dir.mkdir(parents=True)
        converted = output_dir / "working.xlsx"
        workbook = load_workbook(source_copy)
        try:
            workbook["Summary"].title = "Changed"
            workbook.save(converted)
        finally:
            workbook.close()
        return converted

    monkeypatch.setattr(render_module, "_convert_with_libreoffice", fake_convert)

    def fail_publish(*_: object) -> Path:
        raise OSError("disk unavailable")

    monkeypatch.setattr(
        render_module,
        "_publish_recalculation_failure_artifact",
        fail_publish,
    )

    with pytest.raises(RecalculationIntegrityError) as caught:
        recalculate_workbook(source, source)

    assert source.read_bytes() == before
    assert caught.value.evidence["sheet_inventory_integrity"]["matched"] is False
    assert caught.value.evidence["failure_artifact_path"] is None
    assert caught.value.evidence["failure_artifact_error_type"] == "OSError"


@pytest.mark.skipif(find_libreoffice() is None, reason="LibreOffice is not installed")
def test_libreoffice_recalculation_updates_formula_without_changing_sheet_identity(
    tmp_path: Path,
) -> None:
    source = tmp_path / "formula.xlsx"
    _save_workbook(source, two_sheets=False)

    metadata = recalculate_workbook(source, source)

    recalculated = load_workbook(source, data_only=True, read_only=True)
    try:
        assert recalculated["Data"]["B4"].value == 3
    finally:
        recalculated.close()
    integrity = metadata["sheet_inventory_integrity"]
    assert integrity["matched"] is True
    assert integrity["pre"]["sheets"] == integrity["post"]["sheets"]


@pytest.mark.skipif(find_libreoffice() is None, reason="LibreOffice is not installed")
def test_libreoffice_recalculation_forces_manual_workbook_calculate_all(
    tmp_path: Path,
) -> None:
    source = tmp_path / "manual-formulas.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Model"
    sheet["A1"] = 20
    sheet["B1"] = "=A1+22"
    workbook.calculation.calcMode = "manual"
    workbook.calculation.fullCalcOnLoad = False
    workbook.calculation.forceFullCalc = False
    workbook.save(source)
    workbook.close()

    metadata = recalculate_workbook(source, source)

    recalculated = load_workbook(source, data_only=True, read_only=True)
    try:
        assert recalculated["Model"]["B1"].value == 42
    finally:
        recalculated.close()
    assert metadata["calculation_mode"] == "uno-calculate-all"


@pytest.mark.skipif(find_libreoffice() is None, reason="LibreOffice is not installed")
def test_libreoffice_render_integration_preserves_source(tmp_path: Path) -> None:
    source = tmp_path / "integration.xlsx"
    _save_workbook(source)
    before_hash = sha256_file(source)

    result = render_workbook(source, tmp_path / "rendered", dpi=72)

    assert sha256_file(source) == before_hash
    assert result.pages
    assert result.backend == "libreoffice-headless+pymupdf"
    assert result.version["libreoffice"] != "unknown"
    if result.mode == "per_sheet":
        assert {page.sheet for page in result.pages} == {"Data", "Summary"}
    for page in result.pages:
        assert read_png(page.path).startswith(PNG_SIGNATURE)

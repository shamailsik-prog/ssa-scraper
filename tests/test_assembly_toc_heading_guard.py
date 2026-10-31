"""Assembly PDF TOC detection must not erase legitimate statutory provisions."""

from scraper.parsers.assembly_pdf_sections import (
    filter_assembly_pdf_sections,
    is_table_of_contents_chunk,
)


def test_contents_of_register_is_substantive_text():
    text = (
        "11. The contents of the statutory register shall be available for "
        "inspection during ordinary office hours and on payment of the prescribed fee."
    )
    assert not is_table_of_contents_chunk(text)
    sections = [{"section_number": "11", "section_text": text}]
    assert filter_assembly_pdf_sections(sections) == sections


def test_table_of_contents_mentioned_in_substantive_clause_is_not_toc():
    text = (
        "21. The table of contents shall not form part of this enactment. "
        "Rights and obligations remain enforceable under the following provisions."
    )
    assert not is_table_of_contents_chunk(text)


def test_contents_in_section_title_is_not_standalone_toc_heading():
    text = (
        "12. Contents of register\n"
        "Every authority shall maintain a register of the records prescribed by rules."
    )
    assert not is_table_of_contents_chunk(text)


def test_standalone_toc_heading_is_still_recognized():
    text = "THE GAZETTE\nTABLE OF CONTENTS\n1. Preliminary 1\n2. Definitions 2"
    assert is_table_of_contents_chunk(text)


def test_numbered_toc_without_heading_is_still_recognized():
    text = "1. Preliminary 1\n2. Definitions 2\n3. Penalties 7\n4. Appeals 9"
    assert is_table_of_contents_chunk(text)


def test_bare_contents_heading_is_still_recognized():
    assert is_table_of_contents_chunk("CONTENTS")

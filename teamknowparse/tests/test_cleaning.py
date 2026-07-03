from teamknowparse.cleaning import clean_lines, is_mostly_numbers_or_symbols, repeated_header_footer_lines


def test_repeated_header_footer_removed():
    pages = [
        ["Company Confidential", "Title A", "Page 1 of 2"],
        ["Company Confidential", "Title B", "Page 2 of 2"],
    ]
    noise = repeated_header_footer_lines(pages)
    assert "Company Confidential" in noise
    assert clean_lines(pages[0], noise) == ["Title A"]


def test_mostly_numbers_symbols():
    assert is_mostly_numbers_or_symbols("---- 1234 ////")
    assert not is_mostly_numbers_or_symbols("This is meaningful policy text.")


from teamknowparse.classifier import classify_page


def test_form_classification():
    assert classify_page("Name: ______\nDate: ______\nApproved by:") == "FORM"


def test_scanned_classification():
    assert classify_page("", image_area_ratio=0.9) == "SCANNED"


def test_digital_text_classification():
    assert classify_page("This policy explains the process for release approval and review.") == "DIGITAL_PURE_TEXT"


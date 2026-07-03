from teamknowparse.native_extract import _looks_like_heading, extract_native_page


def test_colon_label_is_heading():
    assert _looks_like_heading("Characteristics of IoT :")


def test_wrapped_numbered_list_item_stays_one_item():
    text = """Characteristics of IoT :
1. Dynamic & Self-Adapting: IoT device and system may have the capability to dynamically adapt with
the changing contexts and take actions based on their operating conditions, user’s context, or sensed
environment. For example, consider a surveillance adpt their modes based on the weather it is day or
night, cameras could switch from lower resolution to higher resolution modes when any motion is
detected and alert nearby cameras to do the same.
"""

    elements, _ = extract_native_page(text, "DIGITAL_PURE_TEXT")

    assert elements[0].type == "title"
    assert elements[1].type == "list"
    assert len(elements[1].items) == 1
    item_text = elements[1].items[0].text
    assert "Dynamic & Self-Adapting" in item_text
    assert "changing contexts" in item_text
    assert "night, cameras could switch" in item_text
    assert not any(
        element.type == "paragraph" and element.text and element.text.startswith("the changing contexts")
        for element in elements
    )

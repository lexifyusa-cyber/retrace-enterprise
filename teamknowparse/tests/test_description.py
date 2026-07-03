from teamknowparse.description import create_file_description, create_folder_description, useful_pages_for_description
from teamknowparse.models import Element, FileJson, FolderFile, ListItem, PageJson


def test_description_uses_structural_short_page():
    file_json = FileJson(
        filename="manual.pdf",
        file_path="manual.pdf",
        page_count=1,
        tags=[],
        pages=[
            PageJson(
                page_number=1,
                page_type="DIGITAL_PURE_TEXT",
                elements=[Element(type="title", text="Safety Manual", level=1)],
                rag_text="Safety Manual",
            )
        ],
    )
    assert useful_pages_for_description(file_json, min_chars=300, max_pages=10)
    assert create_file_description(file_json).startswith("manual.pdf is")


def test_description_fallback():
    file_json = FileJson(filename="empty.pdf", file_path="empty.pdf", page_count=2, tags=[], pages=[])
    assert create_file_description(file_json) == "empty.pdf contains 2 processed pages."


def test_description_skips_weak_cover_and_uses_semantic_units():
    file_json = FileJson(
        filename="IOT COMPLETE NOTES.pdf",
        file_path="IOT COMPLETE NOTES.pdf",
        page_count=110,
        tags=[],
        pages=[
            PageJson(
                page_number=1,
                page_type="DIGITAL_PURE_TEXT",
                elements=[
                    Element(type="title", text="DEPARTMENT OF COMPUTER SCIENCE AND", level=1),
                    Element(type="section_header", text="ENGINEERING", level=2),
                    Element(type="section_header", text="INTERNET OF THINGS", level=2),
                    Element(type="section_header", text="NOTES", level=2),
                ],
                rag_text="Title: DEPARTMENT OF COMPUTER SCIENCE AND Section: ENGINEERING Section: INTERNET OF THINGS Section: NOTES",
            ),
            PageJson(
                page_number=2,
                page_type="DIGITAL_PURE_TEXT",
                elements=[
                    Element(type="title", text="INTERNET OF THINGS COURSE OBJECTIVES", level=1),
                    Element(
                        type="paragraph",
                        text=(
                            " Identify IoT elements UNIT I FUNDAMENTALS OF IOT "
                            "UNIT II ELEMENTS OF IOT UNIT III IOT PROTOCOLS "
                            "UNIT IV BUILDING IOT WITH CLOUD AND DATA ANALYTICS "
                            "UNIT V CHALLENGES IN IOT AND CASE STUDIES"
                        ),
                    ),
                ],
                rag_text=(
                    "Title: INTERNET OF THINGS COURSE OBJECTIVES Paragraph:  Identify IoT elements "
                    "UNIT I FUNDAMENTALS OF IOT UNIT II ELEMENTS OF IOT UNIT III IOT PROTOCOLS "
                    "UNIT IV BUILDING IOT WITH CLOUD AND DATA ANALYTICS UNIT V CHALLENGES IN IOT AND CASE STUDIES"
                ),
            ),
        ],
    )

    description = create_file_description(file_json)

    assert description.startswith("IOT COMPLETE NOTES.pdf is a course notes document covering")
    assert "Department Of Computer Science" not in description
    assert "title " not in description.lower()
    assert "section " not in description.lower()
    assert "" not in description
    assert "Fundamentals Of IoT" in description
    assert "Challenges In IoT And Case Studies" in description


def test_description_samples_across_large_files():
    pages = [
        PageJson(
            page_number=1,
            page_type="DIGITAL_PURE_TEXT",
            elements=[Element(type="title", text="Cover Page", level=1)],
            rag_text="Cover Page",
        ),
        PageJson(
            page_number=2,
            page_type="DIGITAL_PURE_TEXT",
            elements=[Element(type="section_header", text="UNIT I Fundamentals", level=2)],
            rag_text="UNIT I Fundamentals " + "introductory material " * 30,
        ),
        PageJson(
            page_number=55,
            page_type="TABLE",
            elements=[Element(type="table", table_description="Protocol Comparison Matrix")],
            rag_text="Table: Protocol Comparison Matrix " + "network protocol details " * 30,
        ),
        PageJson(
            page_number=110,
            page_type="DIGITAL_PURE_TEXT",
            elements=[Element(type="section_header", text="Security Case Studies", level=2)],
            rag_text="Section: Security Case Studies " + "security deployment case study material " * 30,
        ),
    ]
    file_json = FileJson(filename="large.pdf", file_path="large.pdf", page_count=110, tags=[], pages=pages)

    selected = useful_pages_for_description(file_json, min_chars=100, max_pages=3)
    description = create_file_description(file_json, min_chars=100, max_pages=3)

    assert [page.page_number for page in selected] == [2, 55, 110]
    assert "Fundamentals" in description
    assert "Protocol Comparison Matrix" in description
    assert "Security Case Studies" in description


def test_description_deduplicates_low_value_headings_and_lists():
    file_json = FileJson(
        filename="manual.pdf",
        file_path="manual.pdf",
        page_count=4,
        tags=[],
        pages=[
            PageJson(
                page_number=1,
                page_type="DIGITAL_PURE_TEXT",
                elements=[
                    Element(type="title", text="Safety Manual", level=1),
                    Element(type="section_header", text="Safety Manual", level=2),
                    Element(type="section_header", text="Course Outcomes", level=2),
                    Element(
                        type="list",
                        items=[
                            ListItem(text="Hazard analysis", level=0),
                            ListItem(text="Incident reporting", level=0),
                        ],
                    ),
                ],
                rag_text="Title: Safety Manual Section: Safety Manual Section: Course Outcomes List: Hazard analysis; Incident reporting",
            )
        ],
    )

    description = create_file_description(file_json)

    assert description.count("Safety Manual") == 1
    assert "Course Outcomes" not in description
    assert "Hazard analysis" in description
    assert "Incident reporting" in description


def test_folder_description_ranks_structural_topics_and_filters_noise():
    iot_file = FileJson(
        filename="IOT COMPLETE NOTES.pdf",
        file_path="IOT COMPLETE NOTES.pdf",
        page_count=110,
        tags=["iot", "protocols"],
        pages=[
            PageJson(
                page_number=1,
                page_type="DIGITAL_PURE_TEXT",
                elements=[
                    Element(type="title", text="DEPARTMENT OF COMPUTER SCIENCE", level=1),
                    Element(type="section_header", text="NOTES", level=2),
                ],
                rag_text="Title: DEPARTMENT OF COMPUTER SCIENCE Section: NOTES",
            ),
            PageJson(
                page_number=2,
                page_type="DIGITAL_PURE_TEXT",
                elements=[
                    Element(
                        type="paragraph",
                        text=(
                            "UNIT I FUNDAMENTALS OF IOT UNIT II ELEMENTS OF IOT "
                            "UNIT III IOT PROTOCOLS UNIT IV BUILDING IOT WITH CLOUD AND DATA ANALYTICS"
                        ),
                    ),
                ],
                rag_text=(
                    "UNIT I FUNDAMENTALS OF IOT UNIT II ELEMENTS OF IOT "
                    "UNIT III IOT PROTOCOLS UNIT IV BUILDING IOT WITH CLOUD AND DATA ANALYTICS"
                ),
            ),
        ],
    )
    policy_file = FileJson(
        filename="safety_manual.pdf",
        file_path="safety_manual.pdf",
        page_count=4,
        tags=["safety"],
        pages=[
            PageJson(
                page_number=1,
                page_type="TABLE",
                elements=[
                    Element(type="title", text="Safety Manual", level=1),
                    Element(type="section_header", text="Incident Reporting", level=2),
                    Element(type="table", table_description="Hazard Control Matrix"),
                ],
                rag_text="Safety Manual Incident Reporting Hazard Control Matrix " + "response workflow " * 30,
            )
        ],
    )
    folder_files = [
        FolderFile(
            filename=iot_file.filename,
            file_path=iot_file.file_path,
            page_count=iot_file.page_count,
            tags=iot_file.tags,
            file_description=create_file_description(iot_file),
        ),
        FolderFile(
            filename=policy_file.filename,
            file_path=policy_file.file_path,
            page_count=policy_file.page_count,
            tags=policy_file.tags,
            file_description=create_file_description(policy_file),
        ),
    ]

    description = create_folder_description("knowledge", folder_files, [iot_file, policy_file], min_chars=50)

    assert description.startswith("knowledge contains 2 processed files focused on")
    assert "Fundamentals Of IoT" in description
    assert "IoT Protocols" in description
    assert "Incident Reporting" in description
    assert "Hazard Control Matrix" in description
    assert "structured tables" in description
    assert "Department Of Computer Science" not in description
    assert "NOTES" not in description
    assert "processed pages" not in description


def test_folder_description_fallback_for_empty_folder():
    assert create_folder_description("empty", [], []) == "empty contains no processed files."

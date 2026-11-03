"""Supported patch grammar and deterministic line matching (not a compatibility claim)."""

import importlib.util

import pytest


def patch_api():
    assert importlib.util.find_spec("weyra.patch") is not None, "patch parser is not implemented"
    from weyra.patch import apply_hunks, parse_patch

    return parse_patch, apply_hunks


def test_add_delete_move_and_chinese_crlf():
    parse, apply = patch_api()
    operations = parse(
        "*** Begin Patch\r\n*** Add File: 新增.txt\r\n+你好\r\n*** Delete File: old\r\n*** Update File: src\r\n*** Move to: dst\r\n@@\r\n-旧\r\n+新\r\n*** End Patch\r\n"
    )
    assert [op.kind for op in operations] == ["add", "delete", "update"]
    assert operations[0].content == "你好\n"
    assert operations[2].move_to == "dst"
    assert apply("旧\r\n结束\r\n", operations[2].hunks) == "新\r\n结束\r\n"


def test_hunks_use_forward_matching_anchor_and_eof():
    parse, apply = patch_api()
    operation = parse(
        "*** Begin Patch\n*** Update File: a\n@@ heading\n same\n-old\n+first\n@@\n same\n-old\n+last\n*** End of File\n*** End Patch"
    )[0]
    assert (
        apply("heading\nsame\nold\nmiddle\nsame\nold\n", operation.hunks)
        == "heading\nsame\nfirst\nmiddle\nsame\nlast\n"
    )


def test_hunks_preserve_missing_final_newline_and_append_to_empty():
    parse, apply = patch_api()
    operation = parse("*** Begin Patch\n*** Update File: a\n@@\n-old\n+new\n*** End Patch")[0]
    assert apply("old", operation.hunks) == "new"
    insertion = parse("*** Begin Patch\n*** Update File: a\n@@\n+new\n*** End Patch")[0]
    assert apply("", insertion.hunks) == "new\n"


@pytest.mark.parametrize(
    "text",
    [
        "",
        "*** Begin Patch\n*** End Patch",
        "*** Begin Patch\n*** Add File: a\nwrong\n*** End Patch",
        "*** Begin Patch\n*** Delete File: \n*** End Patch",
        "*** Begin Patch\n*** Update File: a\n@@\n*** End Patch",
        "*** Begin Patch\n*** Add File: a\n+x\n*** End Patch\ntrailing",
        "*** Begin Patch\n*** Delete File: a\n*** Move to: b\n*** End Patch",
    ],
)
def test_malformed_patch_rejected(text):
    parse, _ = patch_api()
    with pytest.raises(ValueError):
        parse(text)


def test_missing_context_and_wrong_eof_rejected():
    parse, apply = patch_api()
    operation = parse(
        "*** Begin Patch\n*** Update File: a\n@@\n-x\n+y\n*** End of File\n*** End Patch"
    )[0]
    with pytest.raises(ValueError):
        apply("x\nextra\n", operation.hunks)
    with pytest.raises(ValueError):
        apply("absent\n", operation.hunks)

from textkit.textutils import normalize_space


def test_normalize_space():
    assert normalize_space("  a   b ") == "a b"

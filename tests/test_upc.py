from app.upc import normalize_upc, pad_upc


def test_normalize_strips_leading_zeros():
    assert normalize_upc("012345678901") == normalize_upc("12345678901")


def test_normalize_handles_excel_float_coercion():
    assert normalize_upc("12345678901.0") == "12345678901"


def test_normalize_blank_and_nan():
    assert normalize_upc("") == ""
    assert normalize_upc(None) == ""
    assert normalize_upc("nan") == ""


def test_normalize_strips_non_digits():
    assert normalize_upc("012-345-678-901") == normalize_upc("12345678901")


def test_pad_upc_default_width():
    assert pad_upc("12345") == "000000012345"


def test_pad_upc_blank():
    assert pad_upc("") == ""

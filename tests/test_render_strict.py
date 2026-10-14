import pytest
from app.sender import MissingTemplateVars, render_body


def test_lenient_mode_is_unchanged():
    assert render_body("Hi {{name}} {{area}}", {"name": "Ann"}) == "Hi Ann {{area}}"
    assert render_body("{{x}}", {"x": ""}) == ""


def test_strict_lists_missing_and_blank_vars_sorted():
    with pytest.raises(MissingTemplateVars) as e:
        render_body("{{area}} {{ city }} {{n}} {{area}}", {"city": "  ", "n": 3}, strict=True)
    assert e.value.names == ["area", "city"]


def test_strict_treats_builtin_name_and_email_as_filled():
    assert render_body("Hi {{name}}<{{email}}>", {"name": "", "email": "a@b.gr"}, strict=True) == "Hi <a@b.gr>"


def test_strict_renders_when_all_present():
    assert render_body("{{a}}-{{b}}", {"a": 1, "b": "x"}, strict=True) == "1-x"

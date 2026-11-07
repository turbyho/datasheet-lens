import pytest

from datasheet_lens.errors import Choose, LensError
from datasheet_lens.parametric import Parameter, Quantity, matching_values, parse_filter, quantity, resolve


@pytest.mark.parametrize("text, value, unit", [
    ("25 V", 25.0, "V"),
    ("2.5V, 8V", 2.5, "V"),  # the first quantity: the lowest drive voltage
    ("4.5mOhm @ 20A, 8V", 0.0045, "Ohm"),  # before "@": not the conditions
    ("10 kOhms", 10000.0, "Ohm"),
    ("1.1V @ 250µA", 1.1, "V"),
    ("150µA (Ta)", 0.00015, "A"),
    ("21A (Ta), 60A (Tc)", 21.0, "A"),
    ("1350 pF @ 12.5 V", 1.35e-9, "F"),
    ("+10V, -8V", 10.0, "V"),
    ("-55°C ~ 150°C (TJ)", -55.0, "°C"),
    ("8-VSON-CLIP (3.3x3.3)", 8.0, ""),  # a number with no unit
    ("1 MHz", 1e6, "Hz"),
    ("2 Ω", 2.0, "Ohm"),
])
def test_quantity(text, value, unit):
    q = quantity(text)
    assert q is not None and q.unit == unit and q.value == pytest.approx(value)


def test_quantity_of_a_text_with_no_number():
    assert quantity("-") is None and quantity("N-Channel") is None


def test_parse_filter():
    f = parse_filter("Vdss >= 20 V")
    assert (f.name, f.op, f.values, f.number) == ("Vdss", ">=", ["20 V"], Quantity(20.0, "V"))
    f = parse_filter("channel=N-Channel|P-Channel")
    assert (f.op, f.values, f.number) == ("=", ["N-Channel", "P-Channel"], None)
    f = parse_filter("Rds On (Max) @ Id, Vgs <= 10 mOhm at Vgs=2.5V")
    assert f.name == "Rds On (Max) @ Id, Vgs" and f.number == Quantity(0.01, "Ohm") and f.condition == "Vgs=2.5V"
    assert parse_filter("rds_on <= 10mOhm @ 2.5 V").condition == "2.5 V"
    f = parse_filter("package has 3x3")
    assert (f.op, f.values) == ("has", ["3x3"])
    for wrong, message in (("Vdss 20 V", "give NAME OP VALUE"), (">= 20 V", "empty"),
                           ("Vdss >= twenty", "needs a number")):
        with pytest.raises(LensError, match=message):
            parse_filter(wrong)


def test_matches():
    assert parse_filter("vdss >= 20 V").matches("20 V") and not parse_filter("vdss > 20 V").matches("20 V")
    assert parse_filter("drive <= 2.5V").matches("2.5V, 10V") and not parse_filter("drive <= 2.5V").matches("4.5V")
    assert parse_filter("rds <= 5 mOhm").matches("4.5mOhm @ 20A, 8V")
    assert not parse_filter("rds <= 5 mOhm").matches("-")
    assert not parse_filter("vdss >= 20 V").matches("30 A")  # another unit
    assert parse_filter("vdss = 25V").matches("25 V")
    assert parse_filter("type = n-channel").matches("N-Channel") and not parse_filter("type = N").matches("N-Channel")
    assert parse_filter("package has 3x3|SOT-23").matches("8-PDFN (3.3x3.3)")
    assert parse_filter("package has 3x3|SOT-23").matches("SOT-23-3")


PARAMS = [Parameter("1", "Drain to Source Voltage (Vdss)", [("20 V", "20 V"), ("30 V", "30 V")]),
          Parameter("2", "Supplier Device Package"), Parameter("3", "Package / Case")]


def test_resolve():
    assert resolve("drain to source voltage (vdss)", PARAMS, {}).id == "1"  # the full name
    assert resolve("vds", PARAMS, {"vds": "Drain to Source Voltage (Vdss)"}).id == "1"  # an alias
    assert resolve("Vdss", PARAMS, {}).id == "1"  # the only name that holds the text
    # More parameters: the candidates as a Choose (a normal result of the tools), the
    # parameters of most parts first, with the short names. The tool selects none.
    params = PARAMS + [Parameter("4", "Package Type", [("a", "SMD"), ("b", "THT")], {"a": 7, "b": 2})]
    with pytest.raises(Choose, match="fits more parameters: the search did not run") as e:
        resolve("package", params, {"pkg": "Package / Case"})
    assert e.value.lines[1:] == ['  "Package Type": 9 parts, 2 values',
                                 '  "Supplier Device Package": 0 parts, 0 values',
                                 '  "Package / Case" (short name pkg): 0 parts, 0 values']
    with pytest.raises(LensError, match="no parameter 'foo'"):
        resolve("foo", PARAMS, {})
    assert matching_values(parse_filter("vdss >= 25 V"), PARAMS[0]) == [("30 V", "30 V")]


def test_points_and_conditions():
    from datasheet_lens.parametric import points

    assert points("4mΩ@4.5V, 6mΩ@2.5V") == [(Quantity(0.004, "Ohm"), [Quantity(4.5, "V")]),
                                            (Quantity(0.006, "Ohm"), [Quantity(2.5, "V")])]
    assert points("35mΩ@10V,16A") == [(Quantity(0.035, "Ohm"), [Quantity(10.0, "V"), Quantity(16.0, "A")])]
    assert points("4.5mOhm @ 20A, 8V")[0][1] == [Quantity(20.0, "A"), Quantity(8.0, "V")]
    assert points("25 V") == []  # no "@": no points
    f = parse_filter("rds_on <= 10 mOhm at Vgs<=2.5V")
    assert f.condition_quantity() == ("<=", Quantity(2.5, "V"))
    assert f.matches_at("4mΩ@4.5V, 6mΩ@2.5V") is True
    assert f.matches_at("4mΩ@4.5V, 16mΩ@2.5V") is False
    assert f.matches_at("35mΩ@10V,16A") is None  # no point at 2.5 V or lower
    assert parse_filter("rds_on <= 10 mOhm @ 2.5 V").condition_quantity() == ("=", Quantity(2.5, "V"))
    with pytest.raises(LensError, match="the condition needs a number"):
        parse_filter("rds_on <= 10 mOhm at low Vgs").condition_quantity()
    assert parse_filter("temp >= 125 ℃").number == Quantity(125.0, "°C")


def test_condition_sign_unit_and_kind():
    """A P-channel point at -10 V is not a point at "Vgs<=2.5V". A condition
    needs a unit and a numeric filter."""
    f = parse_filter("rds_on <= 100 mOhm at Vgs<=2.5V")
    assert f.matches_at("50mΩ@-10V,-3A") is None
    p = parse_filter("rds_on <= 400 mOhm at Vgs>=-2.5V")
    assert p.matches_at("50mΩ@-10V, 300mΩ@-2.5V") is True
    with pytest.raises(LensError, match="needs a number with its unit"):
        parse_filter("rds_on <= 10 mOhm at 2.5").condition_quantity()
    with pytest.raises(LensError, match="needs a numeric filter"):
        parse_filter("package has SOT-23 at Vgs=4.5V").condition_quantity()

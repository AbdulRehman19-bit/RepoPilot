from tools.export import export_rows


def test_export_writes_rows(tmp_path):
    path = tmp_path / "out.csv"
    assert export_rows([["a", 1], ["b", 2]], path) == 2
    assert path.read_text() == "a,1\nb,2"

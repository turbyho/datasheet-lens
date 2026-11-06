"""The purchase sources of the project parts ([[part.source]] in the project file)."""

import pytest

from datasheet_lens import project as prj
from datasheet_lens.errors import LensError
from datasheet_lens.store import Store


def test_sources_are_saved_and_read(store_home, tmp_path):
    with Store() as store:
        project = prj.init_project(tmp_path / "board")
        part = prj.add_part(project, store, "CSD16340Q3", manufacturer="Texas Instruments")
    prj.set_source(project, part, "jlcpcb", "C47616", "Extended")
    prj.set_source(project, part, "digikey", " 296-25646-1-ND ")
    text = (project.root / "datasheet-lens.toml").read_text(encoding="utf-8")
    assert '[[part.source]]\nsource = "jlcpcb"\nnumber = "C47616"\nlibrary = "Extended"' in text
    loaded = prj.load(project.root).parts[0]
    assert loaded["sources"] == [{"source": "jlcpcb", "number": "C47616", "library": "Extended"},
                                 {"source": "digikey", "number": "296-25646-1-ND"}]
    # The same source and number: a change, not a second entry. The comments stay.
    path = project.root / "datasheet-lens.toml"
    path.write_text(path.read_text(encoding="utf-8").replace(
        '[[part.source]]\nsource = "jlcpcb"', '# for the JLCPCB assembly\n[[part.source]]\nsource = "jlcpcb"'),
        encoding="utf-8")
    project = prj.load(project.root)
    prj.set_source(project, project.parts[0], "jlcpcb", "C47616", "Basic")
    text = path.read_text(encoding="utf-8")
    assert text.count("[[part.source]]") == 2 and 'library = "Basic"' in text and "# for the JLCPCB assembly" in text


@pytest.mark.parametrize("source, message", [
    ('source = "arrow"\nnumber = "X"', "source must be one of digikey, jlcpcb, mouser"),
    ('source = "jlcpcb"\nnumber = ""', "needs its part number"),
    ('source = "jlcpcb"\nnumber = "C1"\nlibrary = 3', "library must be one of Basic, Preferred, Extended"),
    ('source = "jlcpcb"\nnumber = "C1"\nlibrary = "basic"', "library must be one of Basic, Preferred, Extended"),
])
def test_wrong_sources(store_home, tmp_path, source, message):
    project = prj.init_project(tmp_path / "board")
    path = project.root / "datasheet-lens.toml"
    path.write_text(path.read_text(encoding="utf-8") + f'\n[[part]]\nmpn = "X1"\n\n[[part.source]]\n{source}\n',
                    encoding="utf-8")
    with pytest.raises(LensError, match=message):
        prj.load(project.root)
    with pytest.raises(LensError, match="source must be one of"):
        prj.set_source(project, {"mpn": "X1", "manufacturer": "", "documents": []}, "arrow", "1")

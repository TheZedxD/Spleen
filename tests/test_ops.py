import os
import sys
import zipfile

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import spleen_ops


def test_copy_move_delete_symlink(tmp_path):
    src_dir = tmp_path / "src"
    dest_dir = tmp_path / "dest"
    moved_dir = tmp_path / "moved"
    src_dir.mkdir()
    dest_dir.mkdir()
    moved_dir.mkdir()
    file = src_dir / "file.txt"
    file.write_text("hello")
    link = src_dir / "link"
    link.symlink_to(file)

    spleen_ops.copy_path(str(file), str(dest_dir))
    spleen_ops.copy_path(str(link), str(dest_dir))
    assert (dest_dir / "file.txt").read_text() == "hello"
    assert (dest_dir / "link").is_symlink()
    assert os.readlink(dest_dir / "link") == str(file)

    spleen_ops.move_path(str(dest_dir / "file.txt"), str(moved_dir))
    assert not (dest_dir / "file.txt").exists()
    assert (moved_dir / "file.txt").exists()

    spleen_ops.delete_path(str(dest_dir / "link"))
    assert not (dest_dir / "link").exists()


def test_extract_zip(tmp_path):
    src = tmp_path / "data"
    src.mkdir()
    (src / "a.txt").write_text("a")
    zip_path = tmp_path / "arc.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.write(src / "a.txt", "a.txt")
    out = tmp_path / "out"
    out.mkdir()
    spleen_ops.extract_zip(str(zip_path), str(out))
    assert (out / "a.txt").read_text() == "a"


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root can bypass permissions")
def test_copy_permission_error(tmp_path):
    src = tmp_path / "s.txt"
    src.write_text("hi")
    dest = tmp_path / "dest"
    dest.mkdir(0o400)
    with pytest.raises(PermissionError):
        spleen_ops.copy_path(str(src), str(dest))

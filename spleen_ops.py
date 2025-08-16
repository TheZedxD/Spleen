import os
import shutil
import zipfile


def copy_path(src: str, dest_dir: str) -> None:
    base = os.path.basename(src)
    dest = os.path.join(dest_dir, base)
    if os.path.islink(src):
        target = os.readlink(src)
        os.symlink(target, dest)
    elif os.path.isdir(src):
        shutil.copytree(src, dest, symlinks=True)
    else:
        shutil.copy2(src, dest)


def move_path(src: str, dest_dir: str) -> None:
    base = os.path.basename(src)
    dest = os.path.join(dest_dir, base)
    shutil.move(src, dest)


def delete_path(path: str) -> None:
    if os.path.isdir(path) and not os.path.islink(path):
        shutil.rmtree(path)
    else:
        os.remove(path)


def extract_zip(zip_path: str, dest_dir: str | None = None) -> None:
    if dest_dir is None:
        dest_dir = os.path.dirname(zip_path)
    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(dest_dir)

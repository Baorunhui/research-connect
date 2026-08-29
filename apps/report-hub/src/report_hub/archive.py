from __future__ import annotations

import io
import shutil
import stat
import tempfile
import zipfile
from pathlib import Path


class InvalidReportArchive(ValueError):
    pass


def install_report_zip(data: bytes, target: Path, max_expanded_bytes: int) -> int:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise InvalidReportArchive("body is not a valid ZIP archive") from exc

    infos = archive.infolist()
    if not infos or len(infos) > 5000:
        raise InvalidReportArchive("archive is empty or contains too many files")
    expanded = sum(info.file_size for info in infos)
    if expanded > max_expanded_bytes:
        raise InvalidReportArchive("expanded report exceeds configured limit")

    with tempfile.TemporaryDirectory(dir=target.parent) as temp_name:
        temp = Path(temp_name)
        for info in infos:
            path = Path(info.filename)
            if path.is_absolute() or ".." in path.parts:
                raise InvalidReportArchive("archive contains an unsafe path")
            mode = info.external_attr >> 16
            if stat.S_ISLNK(mode):
                raise InvalidReportArchive("archive may not contain symbolic links")
            destination = temp / path
            if info.is_dir():
                destination.mkdir(parents=True, exist_ok=True)
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source, destination.open("wb") as output:
                shutil.copyfileobj(source, output)
        if not (temp / "index.html").is_file():
            raise InvalidReportArchive("archive root must contain index.html")
        if target.exists():
            shutil.rmtree(target)
        temp.rename(target)
    return expanded


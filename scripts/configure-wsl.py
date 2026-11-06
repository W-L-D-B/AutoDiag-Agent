"""Enable Linux permission metadata in this project's dedicated WSL distribution."""

import os
import platform
import shutil
from configparser import ConfigParser
from pathlib import Path


def main() -> None:
    if "microsoft" not in platform.release().lower():
        print("Native Linux: no DrvFs configuration needed.")
        return
    if os.geteuid() != 0:
        raise PermissionError("Run this helper as root inside AutoDiagUbuntu.")
    path = Path("/etc/wsl.conf")
    config = ConfigParser(interpolation=None)
    config.read(path)
    if not config.has_section("automount"):
        config.add_section("automount")
    options = config.get("automount", "options", fallback="").strip('"')
    values = [value.strip() for value in options.split(",") if value.strip()]
    if "metadata" in values:
        print("DrvFs permission metadata is already configured.")
        return
    backup = path.with_name("wsl.conf.autodiag-backup")
    if path.exists() and not backup.exists():
        shutil.copy2(path, backup)
    values.append("metadata")
    config.set("automount", "options", '"' + ",".join(values) + '"')
    with path.open("w", encoding="utf-8") as stream:
        config.write(stream)
    print("DrvFs metadata enabled; restart only AutoDiagUbuntu to apply it.")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Build an architecture-independent DEB on Linux, without root or network."""
import pathlib
import shutil
import subprocess
import tempfile

root = pathlib.Path(__file__).resolve().parents[1]
dist = root / "dist"
dist.mkdir(exist_ok=True)
with tempfile.TemporaryDirectory(prefix="gs422-package-") as tmp:
    stage = pathlib.Path(tmp)
    for directory in ("DEBIAN", "usr/bin", "usr/lib/gs422", "etc", "lib/systemd/system", "usr/share/doc/gs422"):
        (stage / directory).mkdir(parents=True, exist_ok=True)
    shutil.copytree(root / "gs422", stage / "usr/lib/gs422/gs422", ignore=shutil.ignore_patterns("__pycache__"))
    launcher = stage / "usr/bin/gs422"
    launcher.write_text('#!/usr/bin/python3\nimport sys\nsys.path.insert(0,"/usr/lib/gs422")\nfrom gs422.service import main\nraise SystemExit(main())\n')
    launcher.chmod(0o755)
    shutil.copyfile(root / "config/gs422.conf", stage / "etc/gs422.conf")
    shutil.copyfile(root / "deploy/gs422.service", stage / "lib/systemd/system/gs422.service")
    for name in ("README.md", "docs/PROTOCOL.md", "docs/DOCKER.md", "docs/AT-COMMANDS.md"):
        shutil.copyfile(root / name, stage / "usr/share/doc/gs422" / pathlib.Path(name).name)
    (stage / "DEBIAN/control").write_text('''Package: gs422
Version: 2.2.0
Architecture: all
Maintainer: Gansuan Development
Section: admin
Priority: optional
Depends: python3 (>= 3.10), python3-gi, gir1.2-glib-2.0, network-manager (>= 1.22), systemd
Description: Independent RS422 telemetry and Wi-Fi service
''')
    (stage / "DEBIAN/conffiles").write_text('/etc/gs422.conf\n')
    for name, content in {
        "postinst": 'if [ -d /run/systemd/system ]; then systemctl daemon-reload; fi\n',
        "prerm": 'if [ "$1" = remove ] && [ -d /run/systemd/system ]; then systemctl disable --now gs422.service || true; fi\n',
        "postrm": 'if [ -d /run/systemd/system ]; then systemctl daemon-reload; fi\n',
    }.items():
        p = stage / "DEBIAN" / name
        p.write_text('#!/bin/sh\nset -e\n' + content)
        p.chmod(0o755)
    target = dist / "gs422_2.2.0_all.deb"
    # Do not inherit a Windows checkout's modes or the builder's umask (or tmp 0700).
    stage.chmod(0o755)
    for p in stage.rglob('*'):
        executable = p == launcher or p.parent.name == 'DEBIAN' and p.name in ('postinst', 'prerm', 'postrm')
        p.chmod(0o755 if p.is_dir() or executable else 0o644)
    subprocess.run(["dpkg-deb", "--root-owner-group", "--build", str(stage), str(target)], check=True)
    print(target)

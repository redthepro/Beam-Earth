"""Stable/dev diagnostics policy in ZIPs and frozen staging, without building or installing the game."""
import contextlib
import io
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock
import zipfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import install  # noqa: E402
from tools import build_app, mod_package, package  # noqa: E402


def fixture(root, ver):
    source = root / "mod/beamng_earth"
    for relative in (mod_package.BUILDINFO, mod_package.FACADE, mod_package.NOOP_PROVIDER,
                     mod_package.CAPTURE_PROVIDER):
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((ROOT / "mod/beamng_earth" / relative).read_bytes())
    asset = source / "levels/beamng_earth/info.json"
    asset.parent.mkdir(parents=True)
    asset.write_text('{}', encoding="utf8")
    for relative, content in (("VERSION", ver), ("LICENSE", "license"), ("CREDITS.md", "credits"),
                              ("install.py", "# fixture installer payload"),
                              ("beammp/README.md", "host readme"), ("beammp/Server/BeamNGEarth/main.lua", "return {}"),
                              ("server/earthd.py", "# fixture server")):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf8")
    return source


def contents(path):
    return {p.relative_to(path).as_posix(): p.read_bytes() for p in path.rglob('*') if p.is_file()}


class DiagnosticsPackageTest(unittest.TestCase):
    def assert_payload(self, files, ver):
        self.assertEqual(files[mod_package.BUILDINFO], mod_package.buildinfo(ver))
        self.assertEqual(mod_package.CAPTURE_PROVIDER in files, mod_package.is_dev(ver))
        for relative in (mod_package.FACADE, mod_package.NOOP_PROVIDER):
            self.assertEqual(files[relative], (ROOT / "mod/beamng_earth" / relative).read_bytes())
        self.assertIn("levels/beamng_earth/info.json", files)

    def test_version_policy_matches_client_suffix(self):
        for ver in ("1.8.4-dev.4", "1.8.4.dev.17", "1.7.2-beta.dev.37"):
            self.assertTrue(mod_package.is_dev(ver), ver)
        for ver in (None, "1.8.4", "1.8.4-beta", "1.8.4-dev", "1.8.4-dev.4-extra", "1.8.4-dev.x", "dev.4"):
            self.assertFalse(mod_package.is_dev(ver), ver)

    def test_source_and_beammp_zip_payloads(self):
        for ver in ("1.8.4", "1.8.4-dev.4", "1.8.4-beta.dev.4"):
            with self.subTest(ver=ver), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                source = fixture(root, ver)
                before = contents(source)
                out = root / "dist"
                with mock.patch.object(package, 'ROOT', str(root)), \
                        mock.patch.object(package, 'FILES', ['VERSION', 'install.py', 'LICENSE', 'CREDITS.md']), \
                        mock.patch.object(package, 'TOOLS', []), \
                        mock.patch.object(sys, 'argv', ['package.py', '--out', str(out), '--beammp']), \
                        contextlib.redirect_stdout(io.StringIO()):
                    package.main()
                with zipfile.ZipFile(out / f"BeamNG.Earth-{ver}.zip") as archive:
                    prefix = f"BeamNG.Earth-{ver}/mod/beamng_earth/"
                    files = {name[len(prefix):]: archive.read(name) for name in archive.namelist() if name.startswith(prefix)}
                    self.assert_payload(files, ver)
                    self.assertIsNone(archive.testzip())
                with zipfile.ZipFile(out / f"BeamNG.Earth-BeamMP-Server-{ver}.zip") as outer:
                    with zipfile.ZipFile(io.BytesIO(outer.read("Resources/Client/beamng_earth.zip"))) as archive:
                        self.assert_payload({name: archive.read(name) for name in archive.namelist()}, ver)
                        self.assertIsNone(archive.testzip())
                self.assertEqual(contents(source), before, "packaging must not change source metadata or providers")

    def test_frozen_windows_and_linux_use_separate_selected_tree(self):
        for windows in (True, False):
            for ver in ("1.8.4", "1.8.4-dev.4"):
                with self.subTest(windows=windows, ver=ver), tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    source = fixture(root, ver)
                    before = contents(source)
                    work = root / "build/app"
                    work.mkdir(parents=True)
                    peerwork = types.SimpleNamespace(source_id=lambda server: "fixture-code-id")
                    with mock.patch.object(build_app, 'ROOT', str(root)), \
                            mock.patch.object(build_app, 'IS_WIN', windows), \
                            mock.patch.object(build_app, 'SEP', ';' if windows else ':'), \
                            mock.patch.object(build_app, '_overture_release', return_value="fixture"), \
                            mock.patch.object(build_app.subprocess, 'run') as run, \
                            mock.patch.dict(sys.modules, {'peerwork': peerwork}), \
                            mock.patch.object(sys, 'path', sys.path.copy()):
                        # Linux's former caller omitted ver: the shared stage must also resolve that case.
                        build_app.pyinstaller(str(work), "icon.ico", onefile=windows, ver=ver if windows else None)
                        cmd = run.call_args.args[0]
                        payloads = [cmd[i + 1] for i, value in enumerate(cmd[:-1]) if value == '--add-data']
                        mod_arg = next(value for value in payloads if value.endswith(build_app.SEP + 'mod/beamng_earth'))
                        stage = Path(mod_arg.rsplit(build_app.SEP, 1)[0])
                    self.assertNotEqual(stage, source)
                    self.assertTrue(stage.is_relative_to(work))
                    self.assert_payload(contents(stage), ver)
                    self.assertEqual(contents(source), before)

    def test_stable_install_removes_previous_dev_provider(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = fixture(root / 'source', '1.8.4-dev.4')
            dev = mod_package.stage_mod(source, root / 'dev', '1.8.4-dev.4')
            stable = mod_package.stage_mod(source, root / 'stable', '1.8.4')
            user = root / 'temporary-game-user'
            with mock.patch.object(install, 'MOD_SRC', str(dev)):
                self.assertEqual(install.install_mod(str(user), True, False, quiet=True), 'installed')
            destination = user / 'mods/unpacked' / install.MOD_NAME
            self.assertTrue((destination / mod_package.CAPTURE_PROVIDER).is_file())
            with mock.patch.object(install, 'MOD_SRC', str(stable)):
                self.assertEqual(install.install_mod(str(user), True, False, quiet=True), 'updated')
            self.assert_payload(contents(destination), '1.8.4')

    def test_missing_dev_provider_fails_before_stage_creation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = fixture(root / 'source', '1.8.4-dev.4')
            (source / mod_package.CAPTURE_PROVIDER).unlink()
            with self.assertRaises(FileNotFoundError):
                mod_package.stage_mod(source, root / 'dev', '1.8.4-dev.4')
            self.assertFalse((root / 'dev').exists())
            self.assert_payload(contents(mod_package.stage_mod(source, root / 'stable', '1.8.4')), '1.8.4')

    def test_staging_cannot_mutate_source_or_reuse_dev_destination(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = fixture(root / 'source', '1.8.4-dev.4')
            before = contents(source)
            with self.assertRaises(ValueError):
                mod_package.stage_mod(source, source, '1.8.4')
            with self.assertRaises(ValueError):
                mod_package.stage_mod(source, source / 'nested-stage', '1.8.4')
            stage = mod_package.stage_mod(source, root / 'stage', '1.8.4-dev.4')
            with self.assertRaises(FileExistsError):
                mod_package.stage_mod(source, stage, '1.8.4')
            self.assertEqual(contents(source), before)


if __name__ == '__main__':
    unittest.main()

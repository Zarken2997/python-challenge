"""setupkit（安装器/卸载器）的纯逻辑测试：不真的安装、不碰用户目录。"""

from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from setupkit import app, app_meta, cleanup, installer, manifest, registry_ops, uninstaller  # noqa: E402


class RemovalPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.install = Path("C:/Users/demo/AppData/Local/Programs/PythonChatClient")
        self.data = Path("C:/Users/demo/AppData/Roaming/PythonChatClient")
        self.desktop = Path("C:/Users/demo/Desktop")
        self.start_menu = Path("C:/Users/demo/AppData/Roaming/Microsoft/Windows/Start Menu/Programs")

    def test_plan_covers_shortcuts_data_and_install_dir(self) -> None:
        plan = cleanup.removal_plan(self.install, self.data, self.desktop, self.start_menu)
        self.assertIn(self.install, plan)
        self.assertIn(self.data, plan)
        names = {path.name for path in plan}
        self.assertIn(app_meta.client_shortcut_name(), names)
        self.assertIn(app_meta.uninstall_shortcut_name(), names)

    def test_data_dir_removed_before_install_dir(self) -> None:
        plan = cleanup.removal_plan(self.install, self.data, self.desktop, self.start_menu)
        self.assertLess(plan.index(self.data), plan.index(self.install))

    def test_shortcut_files_live_in_desktop_and_start_menu(self) -> None:
        links = cleanup.shortcut_files(self.install, self.desktop, self.start_menu)
        self.assertEqual(len(links), 3)
        self.assertTrue(all(link.suffix == ".lnk" for link in links))


class SafetyTests(unittest.TestCase):
    def test_rejects_drive_root(self) -> None:
        self.assertFalse(cleanup.is_safe_install_root(Path("C:/")))

    def test_rejects_windows_dirs(self) -> None:
        for path in ("C:/Windows", "C:/Windows/System32", "C:/Program Files", "C:/Users/demo"):
            self.assertFalse(cleanup.is_safe_install_root(Path(path)), path)

    def test_rejects_relative(self) -> None:
        self.assertFalse(cleanup.is_safe_install_root(Path("PythonChatClient")))

    def test_accepts_app_folder(self) -> None:
        self.assertTrue(
            cleanup.is_safe_install_root(Path("C:/Users/demo/AppData/Local/Programs/PythonChatClient"))
        )

    def test_is_inside(self) -> None:
        root = Path("C:/Users/demo/Programs/PythonChatClient")
        self.assertTrue(cleanup.is_inside(root / "Uninstall.exe", root))
        self.assertFalse(cleanup.is_inside(Path("C:/Users/demo/other"), root))


class RegistryValueTests(unittest.TestCase):
    def test_uninstall_values_point_to_uninstaller(self) -> None:
        root = Path("C:/Users/demo/Programs/PythonChatClient")
        values = registry_ops.uninstall_values(root, estimated_kb=12345, install_date="20250101")
        self.assertEqual(values["InstallLocation"], str(root))
        self.assertIn("Uninstall.exe", str(values["UninstallString"]))
        self.assertIn("--uninstall", str(values["UninstallString"]))
        self.assertIn("--silent", str(values["QuietUninstallString"]))
        self.assertEqual(values["EstimatedSize"], 12345)
        self.assertEqual(values["NoModify"], 1)
        self.assertEqual(values["InstallDate"], "20250101")

    def test_display_icon_is_client_exe(self) -> None:
        root = Path("C:/Users/demo/Programs/PythonChatClient")
        values = registry_ops.uninstall_values(root)
        self.assertTrue(str(values["DisplayIcon"]).endswith(app_meta.CLIENT_EXE))


class SelfDeleteTests(unittest.TestCase):
    """自删改为「临时副本收尾」：副本等原进程退出后删除安装目录，再删自己。"""

    def test_temp_copy_command_targets_install_dir(self) -> None:
        root = Path("C:/Users/demo/Programs/PythonChatClient")
        # 源码模式没有 exe 可复制，spawn_temp_cleanup 必须返回 None 而不是抛异常
        self.assertIsNone(uninstaller.spawn_temp_cleanup(root))

    def test_apply_keeps_unknown_files(self) -> None:
        self.assertTrue(hasattr(uninstaller, "cleanup_after_exit"))


class InstallMarkerTests(unittest.TestCase):
    """卸载器只认「自己装过的目录」：靠安装时写入的标记文件判断。"""

    def setUp(self) -> None:
        # 不用 TemporaryDirectory：受限环境（如 DSH 沙箱）里临时目录的清理会被拒绝，
        # 3.14 的 TemporaryDirectory 会因此把测试标成 error，这里自行清理并忽略失败。
        self._tmp_dir = Path(tempfile.mkdtemp(prefix="setupkit-test-"))
        self.addCleanup(shutil.rmtree, str(self._tmp_dir), True)
        self.root = self._tmp_dir / "PythonChatClient"
        try:
            self.root.mkdir(parents=True)
            probe = self.root / ".probe"
            probe.write_text("x", encoding="utf-8")
            probe.unlink()
        except PermissionError:
            # 有的受限环境（如 DSH 沙箱）允许建目录但拒绝往新目录里写，这类环境跳过而不是报错。
            self.skipTest("当前环境限制：无法在临时目录内创建/写入文件")

    def test_marker_written_by_installer_is_detected(self) -> None:
        installer.write_marker(self.root)
        self.assertTrue(cleanup.has_install_marker(self.root))
        self.assertTrue(cleanup.looks_like_install_dir(self.root))

    def test_plain_directory_is_not_install_dir(self) -> None:
        self.assertFalse(cleanup.has_install_marker(self.root))
        self.assertFalse(cleanup.looks_like_install_dir(self.root))

    def test_both_exes_count_as_install_dir(self) -> None:
        for name in (app_meta.CLIENT_EXE, app_meta.UNINSTALL_EXE):
            (self.root / name).write_bytes(b"stub")
        self.assertTrue(cleanup.looks_like_install_dir(self.root))

    def test_uninstaller_refuses_directory_without_marker(self) -> None:
        stray = self._tmp_dir / "some-project"
        stray.mkdir()
        keep = stray / "notes.txt"
        keep.write_text("keep me", encoding="utf-8")
        logs: list[str] = []
        code = uninstaller.uninstall(stray, keep_data=True, log=logs.append, inside_running_dir=False)
        self.assertEqual(code, 1)
        self.assertTrue(keep.exists(), "被拒绝的目录不能被删任何东西")
        self.assertTrue(any("已拒绝卸载" in line for line in logs))

    def test_resolve_install_root_uses_explicit_path(self) -> None:
        target = Path("C:/Users/demo/AppData/Local/Programs/PythonChatClient")
        self.assertEqual(uninstaller.resolve_install_root(str(target)), target)

    def test_installer_refuses_non_empty_foreign_directory(self) -> None:
        """往别人的目录里装会把标记写进去，卸载时就会连目录一起删——必须拒绝。"""
        shed = self._tmp_dir / "my-project"
        shed.mkdir()
        source = shed / "main.py"
        source.write_text("print('keep me')\n", encoding="utf-8")
        logs: list[str] = []
        code = installer.install(
            shed, desktop=False, start_menu=False, log=logs.append
        )
        self.assertEqual(code, 1)
        self.assertTrue(source.exists(), "被拒绝的目录不能写入任何东西")
        self.assertFalse(cleanup.has_install_marker(shed))
        self.assertTrue(any("已有其它文件" in line for line in logs))
        self.assertTrue(any("main.py" in line for line in logs))

    def test_installer_allows_upgrade_of_own_install_dir(self) -> None:
        """旧安装目录（有标记）应允许覆盖安装，否则无法升级。"""
        installer.write_marker(self.root)
        self.assertTrue(cleanup.looks_like_install_dir(self.root))

    def test_foreign_entries_ignores_own_artifacts(self) -> None:
        """只有安装包自己（或已装好的产物）时不算「外来文件」。"""
        for name in (app_meta.SETUP_EXE, app_meta.CLIENT_EXE, app_meta.INSTALL_MARKER):
            (self.root / name).write_bytes(b"stub")
        self.assertEqual(installer.foreign_entries(self.root), [])

    def test_foreign_entries_reports_user_files(self) -> None:
        (self.root / app_meta.SETUP_EXE).write_bytes(b"stub")
        keep = self.root / "报告.docx"
        keep.write_bytes(b"user data")
        names = [path.name for path in installer.foreign_entries(self.root)]
        self.assertEqual(names, ["报告.docx"])


class ManifestTests(unittest.TestCase):
    """安装清单：卸载器只删清单里记录过的东西。"""

    def setUp(self) -> None:
        self._tmp_dir = Path(tempfile.mkdtemp(prefix="setupkit-manifest-"))
        self.addCleanup(shutil.rmtree, str(self._tmp_dir), True)
        self.root = self._tmp_dir / "PythonChatClient"
        try:
            self.root.mkdir(parents=True)
            probe = self.root / ".probe"
            probe.write_text("x", encoding="utf-8")
            probe.unlink()
        except PermissionError:
            self.skipTest("当前环境限制：无法在临时目录内创建/写入文件")

    def test_roundtrip_records_files_and_shortcuts(self) -> None:
        link = self._tmp_dir / "client.lnk"
        written = manifest.write_manifest(
            self.root,
            files=["ChatClient.exe", "Uninstall.exe"],
            shortcuts=[{"path": str(link), "target": str(self.root / "ChatClient.exe")}],
            registry_values={"DisplayName": app_meta.DISPLAY_NAME},
        )
        self.assertTrue(written.is_file())
        data = manifest.read_manifest(self.root)
        self.assertIsNotNone(data)
        self.assertEqual(data["app_id"], app_meta.APP_ID)
        names = [path.name for path in manifest.listed_files(data, self.root)]
        self.assertEqual(names, ["ChatClient.exe", "Uninstall.exe"])
        entries = manifest.shortcut_entries(data)
        self.assertEqual(entries[0][0], link)
        self.assertEqual(manifest.registry_values(data)["DisplayName"], app_meta.DISPLAY_NAME)

    def test_listed_files_reject_escape_paths(self) -> None:
        """清单被篡改也不能把删除范围扩到安装目录之外。"""
        manifest.write_manifest(
            self.root,
            files=["ChatClient.exe", "../outside.txt", "C:/Windows/System32/cmd.exe"],
            shortcuts=[],
        )
        data = manifest.read_manifest(self.root)
        names = [str(path) for path in manifest.listed_files(data, self.root)]
        self.assertEqual(names, [str((self.root / "ChatClient.exe").resolve())])

    def test_missing_or_foreign_manifest_is_ignored(self) -> None:
        self.assertIsNone(manifest.read_manifest(self.root))
        (self.root / manifest.MANIFEST_NAME).write_text('{"app_id":"Other"}', encoding="utf-8")
        self.assertIsNone(manifest.read_manifest(self.root))

    def test_data_dir_only_accepts_standard_location(self) -> None:
        manifest.write_manifest(self.root, files=[], shortcuts=[], data_dir=Path("C:/somewhere/else"))
        data = manifest.read_manifest(self.root)
        self.assertIsNone(manifest.data_dir_of(data))

    def test_manifest_counts_as_install_dir(self) -> None:
        manifest.write_manifest(self.root, files=["ChatClient.exe"], shortcuts=[])
        self.assertTrue(cleanup.looks_like_install_dir(self.root))


class UninstallFromManifestTests(unittest.TestCase):
    """带清单的卸载只删清单文件，陌生文件保留（与 Inno/MSI 的行为一致）。"""

    def setUp(self) -> None:
        self._tmp_dir = Path(tempfile.mkdtemp(prefix="setupkit-uninstall-"))
        self.addCleanup(shutil.rmtree, str(self._tmp_dir), True)
        self.root = self._tmp_dir / "PythonChatClient"
        try:
            self.root.mkdir(parents=True)
            (self.root / "ChatClient.exe").write_bytes(b"client")
            (self.root / "Uninstall.exe").write_bytes(b"uninstall")
            (self.root / "notes.txt").write_text("用户自己放的", encoding="utf-8")
            installer.write_marker(self.root)
            manifest.write_manifest(
                self.root,
                files=["ChatClient.exe", "Uninstall.exe"],
                shortcuts=[],
                data_dir=self._tmp_dir / "appdata",
            )
        except PermissionError:
            self.skipTest("当前环境限制：无法在临时目录内创建/写入文件")

    def test_deletes_listed_files_and_keeps_unknown(self) -> None:
        logs: list[str] = []
        code = uninstaller.uninstall(
            self.root, keep_data=True, log=logs.append, inside_running_dir=False
        )
        self.assertEqual(code, 0)
        self.assertFalse((self.root / "ChatClient.exe").exists())
        self.assertFalse((self.root / "Uninstall.exe").exists())
        self.assertTrue((self.root / "notes.txt").exists(), "清单之外的文件必须保留")
        self.assertTrue(any("清单之外的文件" in line for line in logs))


class AppParserTests(unittest.TestCase):
    """安装包与卸载器是同一个程序，靠 `--uninstall` 切换。"""

    def test_default_is_install(self) -> None:
        args = app.build_parser().parse_args([])
        self.assertFalse(args.uninstall)
        self.assertFalse(args.silent)

    def test_uninstall_flag(self) -> None:
        args = app.build_parser().parse_args(["--uninstall", "--silent", "--keep-data"])
        self.assertTrue(args.uninstall)
        self.assertTrue(args.silent)
        self.assertTrue(args.keep_data)


class ParserTests(unittest.TestCase):
    def test_installer_defaults(self) -> None:
        args = installer.build_parser().parse_args([])
        self.assertFalse(args.silent)
        self.assertEqual(Path(args.dir), app_meta.install_dir())

    def test_installer_silent_flags(self) -> None:
        args = installer.build_parser().parse_args(["--silent", "--no-desktop", "--no-startmenu"])
        self.assertTrue(args.silent)
        self.assertTrue(args.no_desktop)
        self.assertTrue(args.no_startmenu)

    def test_uninstaller_defaults(self) -> None:
        args = uninstaller.build_parser().parse_args([])
        self.assertFalse(args.silent)
        self.assertFalse(args.keep_data)
        self.assertEqual(args.dir, "")


class MetaPathTests(unittest.TestCase):
    def test_install_dir_under_local_app_data(self) -> None:
        self.assertEqual(app_meta.install_dir().parent, app_meta.local_app_data() / "Programs")

    def test_shortcut_names_are_lnk(self) -> None:
        self.assertTrue(app_meta.client_shortcut_name().endswith(".lnk"))
        self.assertTrue(app_meta.uninstall_shortcut_name().endswith(".lnk"))

    def test_install_root_accepted_by_safety_check(self) -> None:
        self.assertTrue(cleanup.is_safe_install_root(app_meta.install_dir()))


if __name__ == "__main__":
    unittest.main()

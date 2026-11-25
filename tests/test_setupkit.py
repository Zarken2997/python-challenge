"""setupkit（安装器/卸载器）的纯逻辑测试：不真的安装、不碰用户目录。"""

from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from setupkit import app_meta, cleanup, installer, registry_ops, uninstaller  # noqa: E402


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
        self.assertIn("--silent", str(values["QuietUninstallString"]))
        self.assertEqual(values["EstimatedSize"], 12345)
        self.assertEqual(values["NoModify"], 1)
        self.assertEqual(values["InstallDate"], "20250101")

    def test_display_icon_is_client_exe(self) -> None:
        root = Path("C:/Users/demo/Programs/PythonChatClient")
        values = registry_ops.uninstall_values(root)
        self.assertTrue(str(values["DisplayIcon"]).endswith(app_meta.CLIENT_EXE))


class SelfDeleteScriptTests(unittest.TestCase):
    def test_script_removes_install_dir_and_itself(self) -> None:
        root = Path("C:/Users/demo/Programs/PythonChatClient")
        script = uninstaller.self_delete_script(root)
        self.assertIn('rmdir /s /q "{}"'.format(root), script)
        self.assertIn('del "%~f0"', script)

    def test_script_waits_before_deleting(self) -> None:
        script = uninstaller.self_delete_script(Path("C:/tmp/app"))
        self.assertIn("timeout /t", script)


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
        self.assertTrue(any("不是空的" in line for line in logs))

    def test_installer_allows_upgrade_of_own_install_dir(self) -> None:
        """旧安装目录（有标记）应允许覆盖安装，否则无法升级。"""
        installer.write_marker(self.root)
        self.assertTrue(cleanup.looks_like_install_dir(self.root))


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

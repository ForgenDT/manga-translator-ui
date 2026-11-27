import _bootstrap  # noqa: F401
import asyncio
from types import SimpleNamespace

import app_logic
from core.config_models import CliSettings as UiCliConfig
from manga_translator.config import CliConfig as BackendCliConfig, Config
from manga_translator.manga_translator import MangaTranslator
from manga_translator.utils import batch_skip
from ui.main_page import runtime


def test_cli_config_has_export_psd_only_default_false():
    assert BackendCliConfig().export_psd_only is False
    assert UiCliConfig().export_psd_only is False


def test_validate_runtime_api_requirements_skips_for_export_psd_only():
    fake_logic = SimpleNamespace()
    cfg = SimpleNamespace(cli=SimpleNamespace(export_psd_only=True))
    assert app_logic.MainAppLogic._validate_runtime_api_requirements(fake_logic, cfg) is True


def test_validate_photoshop_requirement_blocks_when_missing(monkeypatch):
    import manga_translator.utils.photoshop_export as ps_mod
    from PyQt6.QtWidgets import QMessageBox

    warnings = []
    monkeypatch.setattr(ps_mod, "find_photoshop_executable", lambda: None)
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a: warnings.append(a)))

    fake_logic = SimpleNamespace(_t=lambda k, **kw: k, _ui_log=lambda *a: None)

    # Blocks when export_editable_psd=True and Photoshop missing
    cfg_psd = SimpleNamespace(cli=SimpleNamespace(export_editable_psd=True, export_psd_only=False, psd_script_only=False))
    assert app_logic.MainAppLogic._validate_photoshop_requirement(fake_logic, cfg_psd) is False
    assert len(warnings) == 1

    # Blocks when export_psd_only=True and Photoshop missing
    cfg_only = SimpleNamespace(cli=SimpleNamespace(export_editable_psd=False, export_psd_only=True, psd_script_only=False))
    assert app_logic.MainAppLogic._validate_photoshop_requirement(fake_logic, cfg_only) is False
    assert len(warnings) == 2

    # Allows when psd_script_only=True
    cfg_script = SimpleNamespace(cli=SimpleNamespace(export_editable_psd=True, export_psd_only=True, psd_script_only=True))
    assert app_logic.MainAppLogic._validate_photoshop_requirement(fake_logic, cfg_script) is True

    # Allows when Photoshop is found
    monkeypatch.setattr(ps_mod, "find_photoshop_executable", lambda: "C:/Photoshop.exe")
    assert app_logic.MainAppLogic._validate_photoshop_requirement(fake_logic, cfg_psd) is True


def test_workflow_mode_ui_handlers_support_export_psd_only():
    class _FakeCombo:
        def __init__(self):
            self._idx = 0

        def blockSignals(self, _):
            pass

        def setCurrentIndex(self, idx):
            self._idx = idx

        def currentIndex(self):
            return self._idx

    class _FakeLabel:
        def __init__(self):
            self.text = ""

        def setText(self, text):
            self.text = text

    cfg = SimpleNamespace(
        cli=SimpleNamespace(
            load_text=False,
            translate_json_only=False,
            template=False,
            generate_and_export=False,
            colorize_only=False,
            upscale_only=False,
            inpaint_only=False,
            replace_translation=False,
            export_psd_only=False,
        )
    )
    saved = []
    view = SimpleNamespace(
        _t=lambda key, **kw: key.format(**kw) if kw else key,
        workflow_mode_combo=_FakeCombo(),
        start_button=_FakeLabel(),
        translation_page_title=_FakeLabel(),
        translation_page_subtitle=_FakeLabel(),
        config_service=SimpleNamespace(
            get_config=lambda: cfg,
            set_config=lambda c: saved.append(c),
            save_config_file=lambda: saved.append("saved"),
        ),
        controller=SimpleNamespace(
            state_manager=SimpleNamespace(is_translating=lambda: False)
        ),
        update_start_button_text=lambda: runtime.update_start_button_text(view),
    )

    runtime.on_workflow_mode_changed(view, 9)
    assert cfg.cli.export_psd_only is True
    assert view.start_button.text == "Start PSD Generation"
    assert view.translation_page_title.text == "Generate PSD Only"

    view.workflow_mode_combo.setCurrentIndex(0)
    runtime.sync_workflow_mode_from_config(view)
    assert view.workflow_mode_combo.currentIndex() == 9


def test_export_psd_from_local_json_and_batch_skip(tmp_path, monkeypatch):
    import manga_translator.manga_translator as mt_mod
    import manga_translator.utils.photoshop_export as ps_mod

    img_path = tmp_path / "page1.png"
    img_path.write_bytes(b"fake")
    json_path = tmp_path / "page1.json"
    json_path.write_text("{}", encoding="utf-8")
    psd_out = tmp_path / "page1.psd"

    exported_calls = []

    def fake_photoshop_export(
        output_file, ctx, default_font=None, image_path=None,
        verbose=False, result_path_fn=None, line_spacing=None, script_only=False
    ):
        exported_calls.append((output_file, ctx.text_regions, image_path, script_only))

    monkeypatch.setattr(mt_mod, "find_json_path", lambda p: str(json_path))
    monkeypatch.setattr(ps_mod, "get_psd_output_path", lambda p: str(psd_out))
    monkeypatch.setattr(ps_mod, "photoshop_export", fake_photoshop_export)
    monkeypatch.setattr(ps_mod, "resolve_photoshop_font", lambda cfg: "Arial")

    translator = MangaTranslator.__new__(MangaTranslator)
    translator.verbose = False
    translator.export_psd_only = True
    translator.translate_json_only = False
    translator.template = False
    translator.save_text = False
    translator.generate_and_export = False
    translator._result_path = lambda p: p
    translator._check_cancelled = lambda: None
    translator._notify_file_completed = lambda ctx: None

    async def _noop_progress(_):
        return None

    translator._report_progress = _noop_progress
    translator._load_text_and_regions_from_file = lambda p, c: (["region1"], None, False, True, False, 0)

    cfg = Config()
    cfg.cli.export_psd_only = True
    results = asyncio.run(translator._export_psd_from_local_json([(str(img_path), cfg)]))

    assert len(results) == 1
    assert results[0].success is True
    assert results[0].output_path == str(psd_out)
    assert exported_calls == [(str(psd_out), ["region1"], str(img_path), False)]

    # batch_skip skips when overwrite=False and target PSD already exists
    psd_out.write_bytes(b"psd")
    decision = batch_skip._skip_decision(translator, str(img_path), cfg, {"overwrite": False})
    assert decision is not None
    assert decision.reason == "existing_psd"


def test_photoshop_export_handles_missing_dst_points_and_webp(tmp_path, monkeypatch):
    import numpy as np
    from PIL import Image
    import manga_translator.utils.photoshop_export as ps_mod
    from manga_translator.utils import Context

    webp_path = tmp_path / "page.webp"
    Image.new("RGB", (64, 64), color=(255, 255, 255)).save(webp_path, format="WEBP")

    region = SimpleNamespace(
        text=["orig"],
        translation="Bonjour",
        min_rect=np.array([[[10, 10], [50, 10], [50, 30], [10, 30]]], dtype=np.float32),
        font_size=20,
        fg_colors=(0, 0, 0),
        direction="h",
        horizontal=True,
        alignment="center",
        angle=0,
        rich_text=None,
    )
    ctx = Context()
    ctx.text_regions = [region]
    ctx.img_inpainted = None

    out_psd = tmp_path / "out.psd"
    monkeypatch.setattr(ps_mod, "logger", ps_mod.logger)
    ps_mod.photoshop_export(str(out_psd), ctx, default_font="Ames::Regular", image_path=str(webp_path), script_only=True)

    jsx_saved = tmp_path / "manga_translator_work" / "psd" / "page_photoshop_script.jsx"
    assert jsx_saved.exists()
    content = jsx_saved.read_text(encoding="utf-8")
    assert "page.webp" in content
    assert ".ps_original.png" not in content
    assert "Bonjour" in content
    assert "LOG_FILE_PATH" in content
    assert "targetStyle" in content
    assert "Ames::Regular" in content

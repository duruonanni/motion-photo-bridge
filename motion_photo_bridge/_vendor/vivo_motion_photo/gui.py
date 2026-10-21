"""Tkinter desktop interface for Android Motion Photo Merger.

The GUI intentionally contains no media processing logic.  It calls the
``workflow`` module on background threads and only updates Tk widgets on the
main thread.
"""

from __future__ import annotations

import importlib
import inspect
import os
import queue
import threading
import traceback
from collections import Counter
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from . import paths as pathio

try:  # Static reference also lets PyInstaller discover the workflow module.
    from . import workflow as _WORKFLOW
except ImportError:  # A clearer error is raised when the user starts an action.
    _WORKFLOW = None

try:  # Keep recovery available in the packaged GUI without dynamic hidden imports.
    from . import source_actions as _SOURCE_ACTIONS
except ImportError:
    _SOURCE_ACTIONS = None


APP_TITLE = "Android Motion Photo Merger"
POLL_INTERVAL_MS = 80
SOURCE_ACTION_KEEP = "keep"
SOURCE_ACTION_ARCHIVE = "archive"
SOURCE_ACTION_DELETE = "delete"


def _field(record: object, *names: str, default: Any = None) -> Any:
    """Read a field from a dataclass/object or mapping."""

    if isinstance(record, Mapping):
        for name in names:
            if name in record:
                return record[name]
        return default
    for name in names:
        if hasattr(record, name):
            return getattr(record, name)
    return default


def _enum_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, Enum):
        return str(value.value)
    return str(value)


def _status_key(record: object) -> str:
    return _enum_text(_field(record, "status", default="")).replace("_", "").lower()


def _merge_status_text(record: object) -> str:
    """Return the merge column value while accepting legacy records."""

    value = _enum_text(_field(record, "merge_status", default=""))
    if value and value.replace("_", "").lower() != "notstarted":
        return value
    legacy = _enum_text(_field(record, "status", default=""))
    return legacy or value


def _source_action_status_text(record: object) -> str:
    """Qualify generic source outcomes with the requested action."""

    status = _enum_text(_field(record, "source_action_status", default=""))
    normalized = status.replace("_", "").lower()
    if not normalized or normalized == "notrequested":
        return ""
    action = _enum_text(_field(record, "source_action", default=""))
    if normalized in {"pending", "cancelled", "failed", "recoveryrequired"}:
        return f"{action}{status}" if action else status
    return status


def _record_display_status(record: object) -> str:
    """Return the combined result without collapsing source-action failures."""

    value = _field(record, "display_status", default="")
    if value:
        return str(value)
    merge = _merge_status_text(record)
    source = _source_action_status_text(record)
    return " / ".join(part for part in (merge, source) if part)


def _record_has_failure(record: object) -> bool:
    value = _field(record, "has_failure", default=None)
    if value is not None:
        return bool(value)
    merge = _merge_status_text(record).replace("_", "").lower()
    source = _enum_text(
        _field(record, "source_action_status", default="")
    ).replace("_", "").lower()
    return merge == "failed" or source in {"failed", "recoveryrequired"}


def _result_tag(record: object) -> str:
    if _record_has_failure(record):
        return "failed"
    merge = _merge_status_text(record).replace("_", "").lower()
    if merge in {"converted", "existingverified"}:
        return "converted"
    if merge == "skipped":
        return "skipped"
    return _status_key(record)


def _path_text(value: object) -> str:
    return "" if value is None else os.fspath(value)


def _record_selected(record: object) -> bool:
    selected = _field(record, "selected", default=None)
    if selected is not None:
        return bool(selected)
    return _status_key(record) == "exact"


def _set_record_selected(record: object, selected: bool) -> bool:
    """Set ``selected`` when the workflow record permits mutation.

    PairRecord is mutable, but the fallback keeps this module friendly to
    mapping-based records used by small integrations and tests.
    """

    try:
        if isinstance(record, dict):
            record["selected"] = selected
        else:
            setattr(record, "selected", selected)
        return True
    except (AttributeError, TypeError):
        return False


def _set_record_field(record: object, name: str, value: object) -> None:
    """Set one workflow-record field or fail before the GUI reports success."""

    try:
        if isinstance(record, dict):
            record[name] = value
        else:
            setattr(record, name, value)
    except (AttributeError, TypeError) as exc:
        raise ValueError(f"无法更新配对记录字段 {name}") from exc


def _probable_status_like(current: object) -> object:
    """Return ``Probable`` while preserving an Enum-backed status type."""

    if isinstance(current, Enum):
        status_type = type(current)
        try:
            return status_type("Probable")
        except (TypeError, ValueError):
            try:
                return status_type["PROBABLE"]
            except (KeyError, TypeError):
                pass
    return "Probable"


def _validate_manual_pair_candidate(record: object, video_path: Path | str) -> Path:
    """Validate the non-media safety rules for a user-selected MP4."""

    if _status_key(record) not in {"ambiguous", "missingpair"}:
        raise ValueError("只能为 Ambiguous 或 MissingPair 照片人工指定视频")
    image_value = _field(record, "image_path", "image", "photo_path")
    if not image_value:
        raise ValueError("所选记录没有照片，无法人工配对")
    candidate = pathio.resolve_path(Path(video_path).expanduser(), strict=False)
    if candidate.suffix.casefold() != ".mp4" or not pathio.is_file(candidate):
        raise ValueError("请选择一个现存的 MP4 文件")
    if _same_directory(Path(image_value), candidate):
        raise ValueError("视频不能与照片指向同一个文件")
    return candidate


def _probe_manual_video(video_path: Path) -> tuple[int, list[str]]:
    """Read presentation time without making parsing success a pairing claim.

    Android Motion Photo explicitly permits ``-1`` when the presentation
    timestamp is unknown, so an otherwise deliberate manual pairing remains
    reviewable even when vivo timing metadata cannot be parsed.
    """

    warnings: list[str] = []
    try:
        from .mp4 import get_presentation_timestamp_us, probe_mp4

        info = probe_mp4(video_path)
        timestamp_us = int(get_presentation_timestamp_us(info))
    except (ImportError, OSError, TypeError, ValueError) as exc:
        timestamp_us = -1
        warnings.append(f"无法解析 MP4 展示时间，将写入 -1：{exc}")
    else:
        if timestamp_us < 0:
            timestamp_us = -1
            warnings.append("MP4 中没有可用的展示时间，将写入 -1")
    return timestamp_us, warnings


def _apply_manual_pair(
    record: object,
    video_path: Path,
    presentation_timestamp_us: int,
    probe_warnings: Sequence[str] = (),
) -> None:
    """Apply a reviewed manual association without selecting it for conversion."""

    # Revalidate immediately before mutation so this helper is safe when used
    # independently in tests or by a future alternate GUI action.
    candidate = _validate_manual_pair_candidate(record, video_path)
    current_status = _field(record, "status", default="")
    existing_warnings = _field(record, "warnings", default=()) or ()
    if isinstance(existing_warnings, str):
        warnings = [existing_warnings]
    else:
        warnings = [str(item) for item in existing_warnings if item]
    review_warning = (
        "视频由用户人工指定，仍属于 Probable；必须勾选此项并启用“允许转换可能配对”后才会转换"
    )
    if review_warning not in warnings:
        warnings.append(review_warning)
    for warning in probe_warnings:
        warning_text = str(warning)
        if warning_text and warning_text not in warnings:
            warnings.append(warning_text)

    _set_record_field(record, "video_path", candidate)
    _set_record_field(record, "presentation_timestamp_us", int(presentation_timestamp_us))
    _set_record_field(record, "reason", f"用户人工指定 MP4：{candidate.name}")
    _set_record_field(record, "warnings", warnings)
    _set_record_field(record, "status", _probable_status_like(current_status))
    _set_record_field(record, "selected", False)


def _record_reason(record: object) -> str:
    merge_reason = str(_field(record, "merge_reason", default="") or "")
    source_reason = str(_field(record, "source_action_reason", default="") or "")
    split_reasons = [item for item in (merge_reason, source_reason) if item]
    if split_reasons:
        reason = "；".join(dict.fromkeys(split_reasons))
    else:
        reason = str(_field(record, "reason", "message", default="") or "")
    warnings = _field(record, "warnings", default=()) or ()
    if isinstance(warnings, str):
        warning_text = warnings
    else:
        warning_text = "；".join(str(item) for item in warnings if item)
    if reason and warning_text:
        return f"{reason}；{warning_text}"
    return reason or warning_text


def _same_directory(left: Path, right: Path) -> bool:
    """Compare paths without requiring either path to exist."""

    return pathio.path_key(left) == pathio.path_key(right)


def _load_workflow() -> object:
    if _WORKFLOW is not None:
        return _WORKFLOW
    package = __package__ or "vivo_motion_photo"
    try:
        return importlib.import_module(".workflow", package)
    except (ImportError, ValueError) as exc:
        raise RuntimeError(f"无法加载转换模块 vivo_motion_photo.workflow：{exc}") from exc


def _load_source_actions() -> object:
    if _SOURCE_ACTIONS is not None:
        return _SOURCE_ACTIONS
    package = __package__ or "vivo_motion_photo"
    try:
        return importlib.import_module(".source_actions", package)
    except (ImportError, ValueError) as exc:
        raise RuntimeError(f"无法加载归档恢复模块 vivo_motion_photo.source_actions：{exc}") from exc


def _recovery_status_text(result: object) -> str:
    return _enum_text(_field(result, "status", default=""))


def _recovery_needs_attention(result: object) -> bool:
    return _recovery_status_text(result).replace("_", "").lower() in {
        "invalid",
        "recoveryrequired",
    }


def _invoke_convert(
    function: Callable[..., Sequence[object]],
    records: Sequence[object],
    *,
    overwrite: bool,
    include_probable: bool,
    delete_sources: bool,
    progress_callback: Callable[..., None],
    cancel_event: threading.Event,
    archive_sources: bool = False,
    input_root: Path | None = None,
) -> Sequence[object]:
    """Call the documented workflow interface with light signature tolerance."""

    options: dict[str, object] = {
        "overwrite": overwrite,
        "include_probable": include_probable,
        "archive_sources": archive_sources,
        "input_root": input_root,
        "delete_sources": delete_sources,
        "progress_callback": progress_callback,
        "cancel_event": cancel_event,
    }
    signature = inspect.signature(function)
    accepts_kwargs = any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in signature.parameters.values()
    )
    if not accepts_kwargs:
        options = {key: value for key, value in options.items() if key in signature.parameters}
    return function(records, **options)


def _source_action_flags(action: str) -> tuple[bool, bool]:
    """Map the mutually exclusive GUI choice to workflow flags.

    The tuple is ``(archive_sources, delete_sources)``.  Rejecting an unknown
    value keeps a corrupted or programmatically changed Tk variable from
    silently selecting a destructive action.
    """

    if action == SOURCE_ACTION_KEEP:
        return False, False
    if action == SOURCE_ACTION_ARCHIVE:
        return True, False
    if action == SOURCE_ACTION_DELETE:
        return False, True
    raise ValueError(f"未知的源文件处理方式：{action}")


def _confirm_delete_sources(master: tk.Misc, delete_sources: bool) -> bool:
    """Require an explicit second confirmation for permanent source deletion."""

    if not delete_sources:
        return True
    return bool(
        messagebox.askyesno(
            APP_TITLE,
            "转换成功后将永久删除对应的源 JPG 和 MP4，且无法恢复。\n\n"
            "仅在合并文件写入并验证成功后才会删除源文件。是否继续？",
            parent=master,
        )
    )


class MotionPhotoApp(ttk.Frame):
    """Main application frame."""

    def __init__(self, master: tk.Tk) -> None:
        super().__init__(master, padding=10)
        self.master = master
        self.input_var = tk.StringVar()
        self.output_var = tk.StringVar()
        self.recursive_var = tk.BooleanVar(value=True)
        self.include_probable_var = tk.BooleanVar(value=False)
        self.overwrite_var = tk.BooleanVar(value=False)
        self.source_action_var = tk.StringVar(value=SOURCE_ACTION_KEEP)
        self.progress_var = tk.DoubleVar(value=0.0)
        self.summary_var = tk.StringVar(value="请选择输入和输出目录，然后扫描。")
        self.count_var = tk.StringVar(value="0 条记录")

        self.records: list[object] = []
        self._row_to_index: dict[str, int] = {}
        self._selected: list[bool] = []
        self._events: queue.Queue[tuple[Any, ...]] = queue.Queue()
        self._cancel_event = threading.Event()
        self._worker: threading.Thread | None = None
        self._busy_action: str | None = None
        self._scanned_input: str | None = None
        self._scanned_output: str | None = None
        self._pending_recovery: list[object] = []
        self._closing = False

        self._build_widgets()
        self._configure_window()
        self.after(POLL_INTERVAL_MS, self._poll_events)

    def _configure_window(self) -> None:
        self.master.title(APP_TITLE)
        self.master.minsize(880, 620)
        self.master.geometry("1120x720")
        self.master.protocol("WM_DELETE_WINDOW", self._on_close)
        self.pack(fill=tk.BOTH, expand=True)

    def _build_widgets(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=4)
        self.rowconfigure(5, weight=1)

        paths = ttk.LabelFrame(self, text="目录", padding=8)
        paths.grid(row=0, column=0, sticky="ew")
        paths.columnconfigure(1, weight=1)

        ttk.Label(paths, text="输入目录：").grid(row=0, column=0, sticky="w", padx=(0, 6), pady=3)
        self.input_entry = ttk.Entry(paths, textvariable=self.input_var)
        self.input_entry.grid(row=0, column=1, sticky="ew", pady=3)
        self.input_browse = ttk.Button(paths, text="浏览…", command=self._choose_input)
        self.input_browse.grid(row=0, column=2, padx=(6, 0), pady=3)

        ttk.Label(paths, text="输出目录：").grid(row=1, column=0, sticky="w", padx=(0, 6), pady=3)
        self.output_entry = ttk.Entry(paths, textvariable=self.output_var)
        self.output_entry.grid(row=1, column=1, sticky="ew", pady=3)
        self.output_browse = ttk.Button(paths, text="浏览…", command=self._choose_output)
        self.output_browse.grid(row=1, column=2, padx=(6, 0), pady=3)

        controls = ttk.Frame(self)
        controls.grid(row=1, column=0, sticky="ew", pady=(8, 6))
        controls.columnconfigure(7, weight=1)

        self.recursive_check = ttk.Checkbutton(
            controls, text="递归扫描", variable=self.recursive_var
        )
        self.recursive_check.grid(row=0, column=0, padx=(0, 12))
        self.probable_check = ttk.Checkbutton(
            controls,
            text="允许转换已勾选的可能配对",
            variable=self.include_probable_var,
        )
        self.probable_check.grid(row=0, column=1, padx=(0, 12))
        self.overwrite_check = ttk.Checkbutton(
            controls, text="覆盖已有输出", variable=self.overwrite_var
        )
        self.overwrite_check.grid(row=0, column=2, padx=(0, 12))

        ttk.Label(controls, text="转换成功后处理源文件：").grid(
            row=1, column=0, sticky="w", pady=(6, 0)
        )
        self.keep_sources_radio = ttk.Radiobutton(
            controls,
            text="保留（默认）",
            variable=self.source_action_var,
            value=SOURCE_ACTION_KEEP,
        )
        self.keep_sources_radio.grid(row=1, column=1, sticky="w", pady=(6, 0))
        self.archive_sources_radio = ttk.Radiobutton(
            controls,
            text="归档到输入目录下“已合并”",
            variable=self.source_action_var,
            value=SOURCE_ACTION_ARCHIVE,
        )
        self.archive_sources_radio.grid(
            row=1, column=2, sticky="w", padx=(0, 12), pady=(6, 0)
        )
        self.delete_sources_radio = ttk.Radiobutton(
            controls,
            text="永久删除",
            variable=self.source_action_var,
            value=SOURCE_ACTION_DELETE,
        )
        self.delete_sources_radio.grid(row=1, column=3, sticky="w", pady=(6, 0))

        self.scan_button = ttk.Button(controls, text="扫描", command=self._start_scan)
        self.scan_button.grid(row=0, column=3, padx=(0, 6))
        self.convert_button = ttk.Button(
            controls, text="开始转换", command=self._start_convert, state=tk.DISABLED
        )
        self.convert_button.grid(row=0, column=4, padx=(0, 6))
        self.cancel_button = ttk.Button(
            controls, text="取消", command=self._cancel, state=tk.DISABLED
        )
        self.cancel_button.grid(row=0, column=5)
        ttk.Label(controls, textvariable=self.count_var).grid(row=0, column=7, sticky="e")

        table_frame = ttk.LabelFrame(
            self, text="扫描结果（双击行或选中后按空格，可勾选/取消）", padding=5
        )
        table_frame.grid(row=2, column=0, sticky="nsew")
        table_frame.columnconfigure(0, weight=1)
        table_frame.rowconfigure(0, weight=1)

        columns = (
            "selected",
            "merge_status",
            "source_status",
            "image",
            "video",
            "output",
            "reason",
        )
        self.tree = ttk.Treeview(
            table_frame, columns=columns, show="headings", selectmode="extended"
        )
        headings = {
            "selected": "转换",
            "merge_status": "合并状态",
            "source_status": "源文件处理",
            "image": "照片",
            "video": "视频",
            "output": "输出",
            "reason": "配对依据 / 警告",
        }
        widths = {
            "selected": 55,
            "merge_status": 115,
            "source_status": 125,
            "image": 190,
            "video": 190,
            "output": 220,
            "reason": 300,
        }
        for name in columns:
            self.tree.heading(name, text=headings[name])
            self.tree.column(
                name,
                width=widths[name],
                minwidth=45,
                stretch=name not in {"selected", "merge_status", "source_status"},
                anchor=(
                    tk.CENTER
                    if name in {"selected", "merge_status", "source_status"}
                    else tk.W
                ),
            )
        self.tree.grid(row=0, column=0, sticky="nsew")
        vertical = ttk.Scrollbar(table_frame, orient=tk.VERTICAL, command=self.tree.yview)
        horizontal = ttk.Scrollbar(table_frame, orient=tk.HORIZONTAL, command=self.tree.xview)
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        self.tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        self.tree.bind("<Double-1>", self._toggle_event)
        self.tree.bind("<space>", self._toggle_event)
        self.tree.bind("<<TreeviewSelect>>", self._on_tree_selection_changed)

        for status, color in {
            "exact": "#167d28",
            "probable": "#986f00",
            "ambiguous": "#a34b00",
            "converted": "#006f99",
            "failed": "#b00020",
            "invalid": "#b00020",
            "skipped": "#666666",
        }.items():
            self.tree.tag_configure(status, foreground=color)

        select_bar = ttk.Frame(self)
        select_bar.grid(row=3, column=0, sticky="ew", pady=(5, 4))
        self.select_exact_button = ttk.Button(
            select_bar, text="仅勾选精确配对", command=self._select_exact, state=tk.DISABLED
        )
        self.select_exact_button.pack(side=tk.LEFT)
        self.clear_button = ttk.Button(
            select_bar, text="清除勾选", command=self._clear_selection, state=tk.DISABLED
        )
        self.clear_button.pack(side=tk.LEFT, padx=(6, 0))
        self.manual_pair_button = ttk.Button(
            select_bar,
            text="为选中照片指定 MP4…",
            command=self._start_manual_pair,
            state=tk.DISABLED,
        )
        self.manual_pair_button.pack(side=tk.LEFT, padx=(12, 0))
        self.recover_button = ttk.Button(
            select_bar,
            text="恢复未完成归档…",
            command=self._start_recovery,
            state=tk.DISABLED,
        )
        self.recover_button.pack(side=tk.LEFT, padx=(12, 0))

        progress_frame = ttk.Frame(self)
        progress_frame.grid(row=4, column=0, sticky="ew", pady=(0, 5))
        progress_frame.columnconfigure(0, weight=1)
        self.progress = ttk.Progressbar(
            progress_frame, variable=self.progress_var, maximum=100.0, mode="determinate"
        )
        self.progress.grid(row=0, column=0, sticky="ew")
        ttk.Label(progress_frame, textvariable=self.summary_var).grid(
            row=1, column=0, sticky="w", pady=(3, 0)
        )

        log_frame = ttk.LabelFrame(self, text="日志 / 结果", padding=5)
        log_frame.grid(row=5, column=0, sticky="nsew")
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        self.log = tk.Text(log_frame, height=7, wrap=tk.WORD, state=tk.DISABLED)
        self.log.grid(row=0, column=0, sticky="nsew")
        log_scroll = ttk.Scrollbar(log_frame, orient=tk.VERTICAL, command=self.log.yview)
        log_scroll.grid(row=0, column=1, sticky="ns")
        self.log.configure(yscrollcommand=log_scroll.set)

    def _choose_input(self) -> None:
        path = filedialog.askdirectory(
            parent=self.master,
            title="选择照片输入目录",
            initialdir=self.input_var.get() or None,
            mustexist=True,
        )
        if path:
            self.input_var.set(path)

    def _choose_output(self) -> None:
        path = filedialog.askdirectory(
            parent=self.master,
            title="选择合并文件输出目录",
            initialdir=self.output_var.get() or self.input_var.get() or None,
            mustexist=False,
        )
        if path:
            self.output_var.set(path)

    def _on_tree_selection_changed(self, _event: tk.Event[tk.Misc] | None = None) -> None:
        if not self._busy_action:
            self._update_action_states()

    def _manual_pair_index(self) -> int | None:
        rows = list(self.tree.selection())
        if len(rows) != 1:
            return None
        index = self._row_to_index.get(rows[0])
        if index is None:
            return None
        record = self.records[index]
        if _status_key(record) not in {"ambiguous", "missingpair"}:
            return None
        if not _field(record, "image_path", "image", "photo_path"):
            return None
        return index

    def _start_manual_pair(self) -> None:
        """Let the user resolve one uncertain photo/video association."""

        if self._busy_action:
            return
        index = self._manual_pair_index()
        if index is None:
            messagebox.showinfo(
                APP_TITLE,
                "请只选择一条带照片的 Ambiguous 或 MissingPair 记录。",
                parent=self.master,
            )
            return
        record = self.records[index]
        image_path = Path(_field(record, "image_path", "image", "photo_path"))
        chosen = filedialog.askopenfilename(
            parent=self.master,
            title=f"为 {image_path.name} 指定 MP4",
            initialdir=os.fspath(image_path.parent),
            filetypes=(("MP4 视频", "*.mp4"), ("所有文件", "*.*")),
        )
        if not chosen:
            return
        try:
            candidate = _validate_manual_pair_candidate(record, chosen)
        except ValueError as exc:
            messagebox.showerror(APP_TITLE, str(exc), parent=self.master)
            return

        self.summary_var.set(f"正在读取人工指定的视频：{candidate.name}")
        self._append_log(f"人工配对：{image_path.name} → {candidate}")
        self._set_busy("manual_pair")

        def worker() -> None:
            try:
                timestamp_us, warnings = _probe_manual_video(candidate)
                self._events.put(
                    ("manual_pair_done", index, candidate, timestamp_us, warnings)
                )
            except Exception as exc:  # displayed on the UI thread
                self._events.put(
                    ("error", "人工配对失败", exc, traceback.format_exc())
                )

        self._worker = threading.Thread(
            target=worker, name="motion-photo-manual-pair", daemon=True
        )
        self._worker.start()

    def _validated_paths(self) -> tuple[Path, Path] | None:
        input_text = self.input_var.get().strip().strip('"')
        output_text = self.output_var.get().strip().strip('"')
        if not input_text:
            messagebox.showerror(APP_TITLE, "请选择输入目录。", parent=self.master)
            return None
        if not output_text:
            messagebox.showerror(APP_TITLE, "请单独指定输出目录。", parent=self.master)
            return None
        input_root = Path(input_text).expanduser()
        output_root = Path(output_text).expanduser()
        if not pathio.is_dir(input_root):
            messagebox.showerror(APP_TITLE, "输入目录不存在或不是目录。", parent=self.master)
            return None
        if _same_directory(input_root, output_root):
            messagebox.showerror(
                APP_TITLE,
                "输出目录不能与输入目录相同。请选择另一个目录。",
                parent=self.master,
            )
            return None
        return input_root, output_root

    def _start_scan(self) -> None:
        paths = self._validated_paths()
        if paths is None:
            return
        input_root, output_root = paths
        self.records.clear()
        self._selected.clear()
        self._clear_tree()
        self.progress_var.set(0)
        self.summary_var.set("正在扫描照片和视频…")
        self._append_log(f"开始扫描：{input_root}")
        self._append_log(f"输出目录：{output_root}")
        self._set_busy("scan")

        recursive = self.recursive_var.get()

        def worker() -> None:
            try:
                workflow = _load_workflow()
                scan = getattr(workflow, "scan_directory")
                source_actions = _load_source_actions()
                inspect_transactions = getattr(
                    source_actions, "inspect_archive_transactions"
                )
                pending_recovery = list(inspect_transactions(input_root))
                records = list(scan(input_root, output_root, recursive=recursive))
                self._events.put(
                    (
                        "scan_done",
                        records,
                        input_root,
                        output_root,
                        pending_recovery,
                    )
                )
            except Exception as exc:  # displayed on the UI thread
                self._events.put(("error", "扫描失败", exc, traceback.format_exc()))

        self._worker = threading.Thread(target=worker, name="motion-photo-scan", daemon=True)
        self._worker.start()

    def _start_convert(self) -> None:
        if not self.records:
            messagebox.showinfo(APP_TITLE, "请先扫描目录。", parent=self.master)
            return
        paths = self._validated_paths()
        if paths is None:
            return
        input_root, output_root = paths
        if (
            self._scanned_input != pathio.path_key(input_root)
            or self._scanned_output != pathio.path_key(output_root)
        ):
            messagebox.showwarning(
                APP_TITLE,
                "输入或输出目录在扫描后发生了变化，请重新扫描。",
                parent=self.master,
            )
            return

        for index, selected in enumerate(self._selected):
            _set_record_selected(self.records[index], selected)
        selected_count = sum(self._selected)
        if selected_count == 0:
            messagebox.showinfo(APP_TITLE, "请至少勾选一条可转换记录。", parent=self.master)
            return

        probable_selected = any(
            selected and _status_key(record) == "probable"
            for record, selected in zip(self.records, self._selected)
        )
        if probable_selected and not self.include_probable_var.get():
            messagebox.showwarning(
                APP_TITLE,
                "已勾选可能配对。请先启用“允许转换已勾选的可能配对”，或取消这些勾选。",
                parent=self.master,
            )
            return

        try:
            archive_sources, delete_sources = _source_action_flags(
                self.source_action_var.get()
            )
        except ValueError as exc:
            messagebox.showerror(APP_TITLE, str(exc), parent=self.master)
            return
        if archive_sources and self._pending_recovery:
            messagebox.showwarning(
                APP_TITLE,
                "输入目录中存在未完成的归档事务。请先点击“恢复未完成归档…”，"
                "恢复后重新扫描，再开始转换。",
                parent=self.master,
            )
            return
        if not _confirm_delete_sources(self.master, delete_sources):
            return

        self._cancel_event = threading.Event()
        self.progress_var.set(0)
        self.summary_var.set(f"准备转换 {selected_count} 条记录…")
        self._append_log(f"开始转换：已勾选 {selected_count} 条记录")
        self._set_busy("convert")
        # Enforce the review selection at the GUI boundary too.  This keeps a
        # workflow-wide include_probable flag from converting unchecked rows.
        records = [
            record
            for record, selected in zip(self.records, self._selected)
            if selected
        ]
        overwrite = self.overwrite_var.get()
        include_probable = self.include_probable_var.get()

        def progress_callback(*args: object, **kwargs: object) -> None:
            index = kwargs.get("index", kwargs.get("current", 0))
            total = kwargs.get("total", selected_count)
            record = kwargs.get("record")
            if args:
                index = args[0]
            if len(args) >= 2:
                total = args[1]
            if len(args) >= 3:
                record = args[2]
            self._events.put(("progress", index, total, record))

        def worker() -> None:
            try:
                workflow = _load_workflow()
                convert = getattr(workflow, "convert_records")
                results = list(
                    _invoke_convert(
                        convert,
                        records,
                        overwrite=overwrite,
                        include_probable=include_probable,
                        archive_sources=archive_sources,
                        input_root=input_root,
                        delete_sources=delete_sources,
                        progress_callback=progress_callback,
                        cancel_event=self._cancel_event,
                    )
                )
                self._events.put(("convert_done", results, self._cancel_event.is_set()))
            except Exception as exc:  # displayed on the UI thread
                self._events.put(("error", "转换失败", exc, traceback.format_exc()))

        self._worker = threading.Thread(target=worker, name="motion-photo-convert", daemon=True)
        self._worker.start()

    def _start_recovery(self) -> None:
        if self._busy_action:
            return
        raw_input = self.input_var.get().strip()
        if not raw_input:
            messagebox.showerror(APP_TITLE, "请先选择输入目录。", parent=self.master)
            return
        try:
            input_root = pathio.resolve_path(Path(raw_input).expanduser(), strict=False)
            if not pathio.is_dir(input_root):
                raise ValueError(f"输入目录不存在或不是目录：{input_root}")
        except (OSError, ValueError) as exc:
            messagebox.showerror(APP_TITLE, str(exc), parent=self.master)
            return
        if not messagebox.askyesno(
            APP_TITLE,
            "工具会核对事务清单和文件指纹：完整归档会被收尾，未完成归档会回滚到源目录。"
            "任何同名占位或身份冲突都不会被覆盖。是否继续？",
            parent=self.master,
        ):
            return

        self.summary_var.set("正在恢复未完成的归档事务…")
        self._append_log(f"开始恢复归档事务：{input_root}")
        self._set_busy("recover")

        def worker() -> None:
            try:
                source_actions = _load_source_actions()
                recover = getattr(source_actions, "recover_archive_transactions")
                inspect_transactions = getattr(
                    source_actions, "inspect_archive_transactions"
                )
                results = list(recover(input_root))
                pending = list(inspect_transactions(input_root))
                self._events.put(("recovery_done", results, pending))
            except Exception as exc:
                self._events.put(("error", "归档恢复失败", exc, traceback.format_exc()))

        self._worker = threading.Thread(
            target=worker,
            name="motion-photo-recovery",
            daemon=True,
        )
        self._worker.start()

    def _cancel(self) -> None:
        if self._busy_action != "convert" or self._cancel_event.is_set():
            return
        self._cancel_event.set()
        self.cancel_button.configure(state=tk.DISABLED)
        self.summary_var.set("正在取消；当前文件安全完成后停止…")
        self._append_log("已请求取消，等待当前文件处理结束。")

    def _toggle_event(self, event: tk.Event[tk.Misc]) -> str:
        if self._busy_action:
            return "break"
        if event.type == tk.EventType.KeyPress:
            rows = list(self.tree.selection())
        else:
            row = self.tree.identify_row(event.y)
            rows = [row] if row else []
        if not rows:
            return "break"
        for row in rows:
            index = self._row_to_index.get(row)
            if index is None:
                continue
            record = self.records[index]
            if not self._record_can_be_selected(record):
                self._append_log(
                    f"不可勾选：{_path_text(_field(record, 'image_path', default='')) or '(无照片)'}"
                )
                continue
            self._selected[index] = not self._selected[index]
            _set_record_selected(record, self._selected[index])
            self._update_row(row, index)
        self._update_count()
        return "break"

    @staticmethod
    def _record_can_be_selected(record: object) -> bool:
        status = _status_key(record)
        image = _field(record, "image_path", "image", "photo_path")
        video = _field(record, "video_path", "video", "mp4_path")
        selectable = _field(record, "selectable", default=None)
        if selectable is not None:
            return bool(image and video and selectable)
        return bool(image and video) and status in {"exact", "probable"}

    def _select_exact(self) -> None:
        for index, record in enumerate(self.records):
            selected = _status_key(record) == "exact" and self._record_can_be_selected(record)
            self._selected[index] = selected
            _set_record_selected(record, selected)
        self._refresh_tree()

    def _clear_selection(self) -> None:
        for index, record in enumerate(self.records):
            self._selected[index] = False
            _set_record_selected(record, False)
        self._refresh_tree()

    def _populate_records(self, records: Iterable[object]) -> None:
        self.records = list(records)
        self._selected = []
        for record in self.records:
            selected = _record_selected(record) and self._record_can_be_selected(record)
            self._selected.append(selected)
            _set_record_selected(record, selected)
        self._refresh_tree()

    def _clear_tree(self) -> None:
        children = self.tree.get_children()
        if children:
            self.tree.delete(*children)
        self._row_to_index.clear()
        self.count_var.set("0 条记录")

    def _refresh_tree(self) -> None:
        selected_rows = {
            self._row_to_index[row]
            for row in self.tree.selection()
            if row in self._row_to_index
        }
        self._clear_tree()
        for index, record in enumerate(self.records):
            row = self.tree.insert(
                "", tk.END, values=self._row_values(index), tags=(_result_tag(record),)
            )
            self._row_to_index[row] = index
            if index in selected_rows:
                self.tree.selection_add(row)
        self._update_count()

    def _update_row(self, row: str, index: int) -> None:
        record = self.records[index]
        self.tree.item(row, values=self._row_values(index), tags=(_result_tag(record),))

    def _row_values(self, index: int) -> tuple[str, ...]:
        record = self.records[index]
        return (
            "[✓]" if self._selected[index] else "[ ]",
            _merge_status_text(record),
            _source_action_status_text(record),
            _path_text(_field(record, "image_path", "image", "photo_path")),
            _path_text(_field(record, "video_path", "video", "mp4_path")),
            _path_text(_field(record, "output_path", "target_path", "output")),
            _record_reason(record),
        )

    def _update_count(self) -> None:
        selected = sum(self._selected)
        self.count_var.set(f"{len(self.records)} 条记录，已勾选 {selected} 条")
        if not self._busy_action:
            self._update_action_states()

    def _merge_results(self, results: Sequence[object]) -> None:
        """Accept workflows that mutate records or return replacement objects."""

        if not results:
            self._refresh_tree()
            return
        by_image: dict[str, object] = {}
        for result in results:
            image = _path_text(_field(result, "image_path", "image", "photo_path"))
            if image:
                by_image[pathio.path_key(image)] = result
        for index, record in enumerate(self.records):
            image = _path_text(_field(record, "image_path", "image", "photo_path"))
            key = pathio.path_key(image) if image else ""
            if key and key in by_image:
                replacement = by_image[key]
                self.records[index] = replacement
                self._selected[index] = (
                    _record_selected(replacement)
                    and self._record_can_be_selected(replacement)
                )
        self._refresh_tree()

    def _handle_progress(self, index: object, total: object, record: object) -> None:
        try:
            current_number = int(index)
        except (TypeError, ValueError):
            current_number = 0
        try:
            total_number = int(total)
        except (TypeError, ValueError):
            total_number = 0
        if total_number > 0:
            self.progress_var.set(max(0.0, min(100.0, current_number * 100.0 / total_number)))
        status = _record_display_status(record) if record else ""
        image = _path_text(_field(record, "image_path", default="")) if record else ""
        filename = Path(image).name if image else ""
        detail = " ".join(part for part in (status, filename) if part)
        self.summary_var.set(
            f"正在转换 {current_number}/{total_number}" + (f"：{detail}" if detail else "")
        )

    def _handle_scan_done(
        self,
        records: Sequence[object],
        input_root: Path,
        output_root: Path,
        pending_recovery: Sequence[object] = (),
    ) -> None:
        self._set_idle()
        self._scanned_input = pathio.path_key(input_root)
        self._scanned_output = pathio.path_key(output_root)
        self._pending_recovery = list(pending_recovery)
        self._populate_records(records)
        counts = Counter(_enum_text(_field(record, "status", default="Unknown")) for record in records)
        details = "，".join(f"{name} {count}" for name, count in sorted(counts.items()))
        self.summary_var.set(f"扫描完成：{len(records)} 条记录" + (f"（{details}）" if details else ""))
        if self._pending_recovery:
            self.summary_var.set(
                self.summary_var.get()
                + f"；发现 {len(self._pending_recovery)} 个未完成归档事务"
            )
        self._append_log(self.summary_var.get())
        for result in self._pending_recovery:
            self._append_log(
                "待恢复 | "
                + " | ".join(
                    part
                    for part in (
                        _recovery_status_text(result),
                        str(_field(result, "transaction_id", default="")),
                        str(_field(result, "reason", default="")),
                    )
                    if part
                )
            )
        self.progress_var.set(0)
        self._update_action_states()

    def _handle_recovery_done(
        self,
        results: Sequence[object],
        pending: Sequence[object],
    ) -> None:
        self._set_idle()
        self._pending_recovery = list(pending)
        if not results:
            summary = "没有发现未完成的归档事务。"
        else:
            summary = f"归档恢复检查完成：{len(results)} 个事务"
        self.summary_var.set(summary)
        self._append_log(summary)
        for result in results:
            self._append_log(
                "恢复 | "
                + " | ".join(
                    part
                    for part in (
                        _recovery_status_text(result),
                        str(_field(result, "transaction_id", default="")),
                        str(_field(result, "reason", default="")),
                    )
                    if part
                )
            )
        if any(_recovery_needs_attention(item) for item in results) or self._pending_recovery:
            messagebox.showwarning(
                APP_TITLE,
                "仍有归档事务需要人工处理。工具未覆盖或删除发生冲突的文件，"
                "请查看日志中的事务目录与原因。",
                parent=self.master,
            )
            self._update_action_states()
            return
        # Recovery may have restored a previously split JPG/MP4 pair.  Rescan
        # from disk so the selectable rows cannot remain stale.
        self.after_idle(self._start_scan)

    def _handle_manual_pair_done(
        self,
        index: int,
        video_path: Path,
        presentation_timestamp_us: int,
        warnings: Sequence[str],
    ) -> None:
        try:
            record = self.records[index]
            _apply_manual_pair(
                record,
                video_path,
                presentation_timestamp_us,
                warnings,
            )
            self._selected[index] = False
        except (IndexError, TypeError, ValueError) as exc:
            self._handle_error("人工配对失败", exc, traceback.format_exc())
            return

        self._set_idle()
        self._refresh_tree()
        timestamp_text = (
            f"{presentation_timestamp_us} µs"
            if presentation_timestamp_us >= 0
            else "-1（未知）"
        )
        summary = (
            f"已人工指定 {video_path.name}；状态为 Probable，展示时间 {timestamp_text}。"
            "请确认后勾选，并启用“允许转换可能配对”。"
        )
        self.summary_var.set(summary)
        self._append_log(summary)
        for warning in warnings:
            self._append_log(f"警告：{warning}")
        self._update_action_states()

    def _handle_convert_done(self, results: Sequence[object], cancelled: bool) -> None:
        self._merge_results(results)
        self._set_idle()
        self.progress_var.set(100 if not cancelled else self.progress_var.get())
        counts = Counter(_record_display_status(record) or "Unknown" for record in results)
        details = "，".join(f"{name} {count}" for name, count in sorted(counts.items()))
        if cancelled:
            summary = "转换已取消"
        else:
            summary = "转换完成"
        if details:
            summary += f"：{details}"
        self.summary_var.set(summary)
        self._append_log(summary)
        for record in results:
            status = _record_display_status(record)
            image = _path_text(_field(record, "image_path", default=""))
            reason = _record_reason(record)
            if _record_has_failure(record) or "skipped" in status.lower() or reason:
                self._append_log(
                    " | ".join(part for part in (status, image, reason) if part)
                )
        self._update_action_states()

    def _handle_error(self, title: str, exc: BaseException, detail: str) -> None:
        self._set_idle()
        self.summary_var.set(f"{title}：{exc}")
        self._append_log(f"{title}：{exc}")
        self._append_log(detail.rstrip())
        messagebox.showerror(APP_TITLE, f"{title}\n\n{exc}", parent=self.master)
        self._update_action_states()

    def _poll_events(self) -> None:
        if self._closing:
            return
        try:
            while True:
                event = self._events.get_nowait()
                kind = event[0]
                if kind == "progress":
                    self._handle_progress(event[1], event[2], event[3])
                elif kind == "scan_done":
                    self._handle_scan_done(
                        event[1],
                        event[2],
                        event[3],
                        event[4] if len(event) > 4 else (),
                    )
                elif kind == "manual_pair_done":
                    self._handle_manual_pair_done(
                        event[1], event[2], event[3], event[4]
                    )
                elif kind == "convert_done":
                    self._handle_convert_done(event[1], event[2])
                elif kind == "recovery_done":
                    self._handle_recovery_done(event[1], event[2])
                elif kind == "error":
                    self._handle_error(event[1], event[2], event[3])
        except queue.Empty:
            pass
        self.after(POLL_INTERVAL_MS, self._poll_events)

    def _set_busy(self, action: str) -> None:
        self._busy_action = action
        for widget in (
            self.input_entry,
            self.output_entry,
            self.input_browse,
            self.output_browse,
            self.recursive_check,
            self.probable_check,
            self.overwrite_check,
            self.keep_sources_radio,
            self.archive_sources_radio,
            self.delete_sources_radio,
            self.scan_button,
            self.convert_button,
            self.select_exact_button,
            self.clear_button,
            self.manual_pair_button,
            self.recover_button,
        ):
            widget.configure(state=tk.DISABLED)
        self.cancel_button.configure(state=tk.NORMAL if action == "convert" else tk.DISABLED)

    def _set_idle(self) -> None:
        self._busy_action = None
        for widget in (
            self.input_entry,
            self.output_entry,
            self.input_browse,
            self.output_browse,
            self.recursive_check,
            self.probable_check,
            self.overwrite_check,
            self.keep_sources_radio,
            self.archive_sources_radio,
            self.delete_sources_radio,
            self.scan_button,
            self.recover_button,
        ):
            widget.configure(state=tk.NORMAL)
        self.cancel_button.configure(state=tk.DISABLED)
        self._update_action_states()

    def _update_action_states(self) -> None:
        if self._busy_action:
            return
        has_records = bool(self.records)
        has_selected = any(self._selected)
        self.convert_button.configure(state=tk.NORMAL if has_records and has_selected else tk.DISABLED)
        self.select_exact_button.configure(state=tk.NORMAL if has_records else tk.DISABLED)
        self.clear_button.configure(state=tk.NORMAL if has_records else tk.DISABLED)
        self.manual_pair_button.configure(
            state=tk.NORMAL if self._manual_pair_index() is not None else tk.DISABLED
        )
        self.recover_button.configure(
            state=tk.NORMAL if self._pending_recovery else tk.DISABLED
        )

    def _append_log(self, text: str) -> None:
        self.log.configure(state=tk.NORMAL)
        self.log.insert(tk.END, text.rstrip() + "\n")
        self.log.see(tk.END)
        self.log.configure(state=tk.DISABLED)

    def _on_close(self) -> None:
        if self._busy_action:
            action_text = "转换" if self._busy_action == "convert" else "归档恢复"
            if not messagebox.askyesno(
                APP_TITLE,
                f"{action_text}仍在进行。现在退出会在下次启动时要求恢复，是否退出？",
                parent=self.master,
            ):
                return
        self._closing = True
        self._cancel_event.set()
        self.master.destroy()


def main() -> int:
    root: tk.Tk | None = None
    try:
        root = tk.Tk()
        style = ttk.Style(root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        MotionPhotoApp(root)
        root.mainloop()
    except tk.TclError as exc:
        # This is mainly useful when invoked in a headless environment.
        try:
            if root is not None:
                root.destroy()
        except tk.TclError:
            pass
        print(f"无法启动图形界面：{exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""主窗口：自选股管理 + 显示/数据设置。"""
from __future__ import annotations

import queue
import threading
import time
import tkinter as tk
from typing import Callable

from .config import CONFIG_PATH
from .market import (
    GOLD_MANUAL_PREFIX,
    GOLD_NOTE,
    GOLD_SGE_SYMBOL,
    market_label,
    resolve_input,
    search_with_fallback,
    status_text,
)
from .platform_win import apply_tool_window
from .ui_common import (
    AMBER,
    GREEN,
    MUTED,
    MUTED_DIM,
    PANEL_BG,
    PANEL_CARD,
    TEXT,
    px,
)

FONT = "Microsoft YaHei UI"


class SettingsWindow:
    def __init__(self, app) -> None:
        self.app = app
        self.store = app.store
        self.win = tk.Toplevel(app.root)
        self.win.title("任务栏股票小组件 · 设置")
        self.win.configure(bg=PANEL_BG)
        self.win.geometry(f"{px(760)}x{px(680)}")
        self.win.minsize(px(700), px(620))
        self.win.protocol("WM_DELETE_WINDOW", self.hide)
        self._refreshing = False
        # 名称搜索：300ms 防抖 + 后台线程 + 队列回主线程，避免阻塞设置界面
        self._search_queue: queue.Queue = queue.Queue()
        self._search_after = None
        self._search_poll = None
        self._search_seq = 0
        self._search_pending = 0
        self._results: list = []
        self._build()
        self._load_from_config()
        self._tick()

    # ---------------------------------------------------------------- helpers
    def _label(self, parent, text, size=11, color=TEXT, bold=False, **kw):
        return tk.Label(
            parent, text=text, bg=parent["bg"], fg=color,
            font=(FONT, px(size), "bold") if bold else (FONT, px(size)),
            anchor="w", **kw,
        )

    def _button(self, parent, text, command: Callable, width=None) -> tk.Button:
        btn = tk.Button(
            parent, text=text, command=command,
            bg="#2a2f3b", fg=TEXT, activebackground="#343a49",
            activeforeground="#ffffff", bd=0, relief="flat",
            font=(FONT, px(10)), padx=px(12), pady=px(5),
            cursor="hand2", highlightthickness=0,
        )
        if width:
            btn.configure(width=width)
        return btn

    # ------------------------------------------------------------------- ui
    def _build(self) -> None:
        outer = tk.Frame(self.win, bg=PANEL_BG, padx=px(18), pady=px(14))
        outer.pack(fill="both", expand=True)

        header = tk.Frame(outer, bg=PANEL_BG)
        header.pack(fill="x")
        tk.Label(header, text="任务栏股票小组件", bg=PANEL_BG, fg=TEXT,
                 font=(FONT, px(16), "bold")).pack(side="left")
        self.status_label = tk.Label(header, text="正在获取行情…", bg=PANEL_BG,
                                     fg=MUTED, font=(FONT, px(10)))
        self.status_label.pack(side="right")
        tk.Label(outer, text="点击「保存并应用」后小组件立即刷新；右键小组件可快速切换模式。",
                 bg=PANEL_BG, fg=MUTED_DIM, font=(FONT, px(9)), anchor="w").pack(fill="x", pady=(px(4), px(12)))

        body = tk.Frame(outer, bg=PANEL_BG)
        body.pack(fill="both", expand=True)

        # ---- 左：搜索添加 + 自选股
        left = tk.Frame(body, bg=PANEL_CARD, padx=px(14), pady=px(12))
        left.pack(side="left", fill="both", expand=True)

        self._label(left, "搜索添加", 13, bold=True).pack(anchor="w")
        self._label(left, "输入名称、代码或拼音首字母，如 中证500 / 000905 / gzmt，"
                    "结果出现后点击或按回车添加。",
                    9, MUTED_DIM).pack(anchor="w", pady=(px(2), px(6)))

        search_row = tk.Frame(left, bg=PANEL_CARD)
        search_row.pack(fill="x")
        self.search_var = tk.StringVar()
        self.search_entry = tk.Entry(
            search_row, textvariable=self.search_var, bg="#15171d", fg=TEXT,
            insertbackground=TEXT, bd=0, relief="flat", font=(FONT, px(11)),
        )
        self.search_entry.pack(side="left", fill="x", expand=True, ipady=px(5))
        self._retry_btn = self._button(search_row, "重试", self._retry_search)
        self.search_entry.bind("<Return>", self._on_search_enter)

        self.search_status = tk.Label(
            left, text="输入关键词开始搜索", bg=PANEL_CARD, fg=MUTED_DIM,
            font=(FONT, px(9)), anchor="w",
        )
        self.search_status.pack(fill="x", pady=(px(4), 0))

        # 结果下拉放在独立容器里：隐藏在搜索框下方，不挤占自选列表的高度
        self.result_wrap = tk.Frame(left, bg=PANEL_CARD)
        self.result_wrap.pack(fill="x", pady=(px(4), 0))
        self.result_box = tk.Listbox(
            self.result_wrap, bg="#15171d", fg=TEXT, bd=0, highlightthickness=0,
            selectbackground="#2d3a55", selectforeground="#ffffff",
            font=(FONT, px(10)), height=5, activestyle="none", relief="flat",
            takefocus=0,
        )
        self.result_box.pack(fill="x")
        self.result_box.bind("<Button-1>", self._on_result_click)
        self.result_box.bind("<Return>", self._on_result_enter)
        self.result_wrap.pack_forget()
        self._result_visible = False
        self.search_var.trace_add("write", lambda *_a: self._schedule_search())

        self.sep_ref = tk.Frame(left, bg="#2a2e38", height=1)
        self.sep_ref.pack(fill="x", pady=(px(10), px(8)))

        self._label(left, "自选股票", 13, bold=True).pack(anchor="w")
        self._label(left, "代码仍可直接输入（备用），与搜索共用同一套去重逻辑。",
                    9, MUTED_DIM).pack(anchor="w", pady=(px(2), px(8)))

        list_wrap = tk.Frame(left, bg=PANEL_CARD)
        list_wrap.pack(fill="both", expand=True)
        scroll = tk.Scrollbar(list_wrap, width=px(8), bg="#232731", troughcolor="#15171d",
                              activebackground="#3a3f4c", bd=0, highlightthickness=0)
        scroll.pack(side="right", fill="y")
        self.listbox = tk.Listbox(
            list_wrap, bg="#15171d", fg=TEXT, bd=0, highlightthickness=0,
            selectbackground="#2d3a55", selectforeground="#ffffff",
            font=(FONT, px(11)), yscrollcommand=scroll.set,
            activestyle="none", relief="flat",
        )
        self.listbox.pack(side="left", fill="both", expand=True)
        scroll.configure(command=self.listbox.yview)

        labels = tk.Frame(left, bg=PANEL_CARD)
        labels.pack(fill="x", pady=(px(8), 0))
        self._label(labels, "代码", 9, MUTED_DIM, width=13).pack(side="left")
        self._label(labels, "别名(可选)", 9, MUTED_DIM).pack(side="left")

        edit_row = tk.Frame(left, bg=PANEL_CARD)
        edit_row.pack(fill="x", pady=(px(3), 0))
        self.entry_symbol = tk.Entry(
            edit_row, bg="#15171d", fg=TEXT, insertbackground=TEXT, bd=0,
            relief="flat", font=(FONT, px(11)), width=12,
        )
        self.entry_symbol.pack(side="left", ipady=px(5), padx=(0, px(6)))
        self.entry_alias = tk.Entry(
            edit_row, bg="#15171d", fg=TEXT, insertbackground=TEXT, bd=0,
            relief="flat", font=(FONT, px(11)), width=8,
        )
        self.entry_alias.pack(side="left", ipady=px(5), padx=(0, px(6)))
        self._button(edit_row, "添加", self._add_symbol).pack(side="left")
        self.entry_symbol.bind("<Return>", lambda e: self._add_symbol())

        ops = tk.Frame(left, bg=PANEL_CARD)
        ops.pack(fill="x", pady=(px(6), 0))
        self._button(ops, "删除", self._remove_selected).pack(side="left", padx=(0, px(6)))
        self._button(ops, "上移", lambda: self._move(-1)).pack(side="left", padx=(0, px(6)))
        self._button(ops, "下移", lambda: self._move(1)).pack(side="left", padx=(0, px(6)))
        self._button(ops, "添加积存金", self._open_gold_dialog).pack(side="left")
        self._label(left, "银行积存金无稳定公开报价，默认使用上金所 Au99.99 参考价并标注；"
                    "也可手动填写固定价格。", 8, MUTED_DIM,
                    wraplength=px(380), justify="left").pack(
            anchor="w", pady=(px(4), 0))

        # ---- 右：设置
        right = tk.Frame(body, bg=PANEL_CARD, padx=px(14), pady=px(12), width=px(320))
        right.pack(side="left", fill="y", padx=(px(12), 0))
        right.pack_propagate(False)
        self._label(right, "显示设置", 13, bold=True).pack(anchor="w", pady=(0, px(8)))

        self.mode_var = tk.StringVar(value="trend")
        row = tk.Frame(right, bg=PANEL_CARD)
        row.pack(fill="x", pady=(0, px(6)))
        self._label(row, "显示模式", 10, MUTED, width=8).pack(side="left")
        for value, label in (("trend", "走势"), ("mini", "迷你")):
            tk.Radiobutton(
                row, text=label, value=value, variable=self.mode_var,
                command=self._apply_options, bg=PANEL_CARD, fg=TEXT,
                selectcolor="#15171d", activebackground=PANEL_CARD,
                activeforeground=TEXT, font=(FONT, px(10)), bd=0,
                highlightthickness=0,
            ).pack(side="left", padx=(0, px(10)))

        self.count_var = tk.StringVar(value="1")
        row = tk.Frame(right, bg=PANEL_CARD)
        row.pack(fill="x", pady=(0, px(6)))
        self._label(row, "同时显示", 10, MUTED, width=8).pack(side="left")
        for value in ("1", "2", "3"):
            tk.Radiobutton(
                row, text=f"{value} 只", value=value, variable=self.count_var,
                command=self._apply_options, bg=PANEL_CARD, fg=TEXT,
                selectcolor="#15171d", activebackground=PANEL_CARD,
                activeforeground=TEXT, font=(FONT, px(10)), bd=0,
                highlightthickness=0,
            ).pack(side="left", padx=(0, px(8)))

        self.style_var = tk.StringVar(value="cn")
        row = tk.Frame(right, bg=PANEL_CARD)
        row.pack(fill="x", pady=(0, px(6)))
        self._label(row, "涨跌配色", 10, MUTED, width=8).pack(side="left")
        for value, label in (("cn", "红涨绿跌"), ("intl", "绿涨红跌")):
            tk.Radiobutton(
                row, text=label, value=value, variable=self.style_var,
                command=self._apply_options, bg=PANEL_CARD, fg=TEXT,
                selectcolor="#15171d", activebackground=PANEL_CARD,
                activeforeground=TEXT, font=(FONT, px(10)), bd=0,
                highlightthickness=0,
            ).pack(side="left", padx=(0, px(8)))

        self.rotate_var = tk.StringVar(value="10")
        self.interval_var = tk.StringVar(value="3")
        row = tk.Frame(right, bg=PANEL_CARD)
        row.pack(fill="x", pady=(0, px(6)))
        self.scale_var = tk.StringVar(value="80%")
        scale_options = [f"{n}%" for n in range(60, 141, 10)]
        option = tk.OptionMenu(row, self.scale_var, *scale_options,
                               command=lambda _v: self._apply_options())
        option.configure(bg="#15171d", fg=TEXT, bd=0, relief="flat",
                         activebackground="#2a2f3b", highlightthickness=0,
                         font=(FONT, px(10)), width=5, anchor="w")
        option["menu"].configure(bg="#20232b", fg=TEXT, bd=0,
                                 activebackground="#3a3f4c",
                                 activeforeground="#ffffff",
                                 font=(FONT, px(10)))
        option.pack(side="left")
        self._label(row, "Ctrl/Shift+滚轮也可缩放", 8, MUTED_DIM).pack(
            side="left", padx=(px(6), 0))

        self.rotate_on_var = tk.BooleanVar(value=True)
        row = tk.Frame(right, bg=PANEL_CARD)
        row.pack(fill="x", pady=(0, px(6)))
        tk.Checkbutton(
            row, text="启用轮播", variable=self.rotate_on_var, command=self._apply_options,
            bg=PANEL_CARD, fg=TEXT, selectcolor="#15171d",
            activebackground=PANEL_CARD, activeforeground=TEXT,
            font=(FONT, px(10)), bd=0, highlightthickness=0,
        ).pack(side="left")
        self._label(row, "关闭时固定显示自选列表第一只", 8, MUTED_DIM).pack(
            side="left", padx=(px(6), 0))

        for label, var, lo, hi, tip in (
            ("轮播间隔", self.rotate_var, 0, 600, "秒，0 表示不轮播"),
            ("刷新间隔", self.interval_var, 2, 60, "秒，建议 3 秒"),
        ):
            row = tk.Frame(right, bg=PANEL_CARD)
            row.pack(fill="x", pady=(0, px(6)))
            self._label(row, label, 10, MUTED, width=8).pack(side="left")
            tk.Spinbox(
                row, from_=lo, to=hi, width=5, textvariable=var,
                bg="#15171d", fg=TEXT, bd=0, relief="flat",
                insertbackground=TEXT, font=(FONT, px(10)),
                command=self._apply_options, buttonbackground="#2a2f3b",
            ).pack(side="left")
            self._label(row, tip, 9, MUTED_DIM).pack(side="left", padx=(px(8), 0))

        self.width_var = tk.StringVar(value="0")
        row = tk.Frame(right, bg=PANEL_CARD)
        row.pack(fill="x", pady=(0, px(6)))
        self._label(row, "最大宽度", 10, MUTED, width=8).pack(side="left")
        tk.Spinbox(
            row, from_=0, to=2000, increment=20, width=5, textvariable=self.width_var,
            bg="#15171d", fg=TEXT, bd=0, relief="flat",
            insertbackground=TEXT, font=(FONT, px(10)),
            command=self._apply_options, buttonbackground="#2a2f3b",
        ).pack(side="left")
        self._label(row, "px，0=自动", 9, MUTED_DIM).pack(side="left", padx=(px(8), 0))

        self.provider_var = tk.StringVar(value="自动")
        row = tk.Frame(right, bg=PANEL_CARD)
        row.pack(fill="x", pady=(0, px(6)))
        self._label(row, "数据源", 10, MUTED, width=8).pack(side="left")
        option = tk.OptionMenu(row, self.provider_var, "自动", "腾讯", "新浪",
                               command=lambda _v: self._apply_options())
        option.configure(bg="#15171d", fg=TEXT, bd=0, relief="flat",
                         activebackground="#2a2f3b", highlightthickness=0,
                         font=(FONT, px(10)), width=6, anchor="w")
        option["menu"].configure(bg="#20232b", fg=TEXT, bd=0,
                                 activebackground="#3a3f4c", activeforeground="#ffffff",
                                 font=(FONT, px(10)))
        option.pack(side="left")

        self.top_var = tk.BooleanVar(value=True)
        row = tk.Frame(right, bg=PANEL_CARD)
        row.pack(fill="x", pady=(px(2), px(2)))
        tk.Checkbutton(
            row, text="窗口置顶", variable=self.top_var, command=self._apply_options,
            bg=PANEL_CARD, fg=TEXT, selectcolor="#15171d",
            activebackground=PANEL_CARD, activeforeground=TEXT,
            font=(FONT, px(10)), bd=0, highlightthickness=0,
        ).pack(side="left")

        self.breadth_var = tk.BooleanVar(value=True)
        row = tk.Frame(right, bg=PANEL_CARD)
        row.pack(fill="x", pady=(px(2), px(2)))
        tk.Checkbutton(
            row, text="显示涨跌家数（涨/跌/平）", variable=self.breadth_var,
            command=self._apply_options, bg=PANEL_CARD, fg=TEXT,
            selectcolor="#15171d", activebackground=PANEL_CARD,
            activeforeground=TEXT, font=(FONT, px(10)), bd=0,
            highlightthickness=0,
        ).pack(side="left")

        self.hidden_var = tk.BooleanVar(value=False)
        row = tk.Frame(right, bg=PANEL_CARD)
        row.pack(fill="x", pady=(px(2), px(2)))
        tk.Checkbutton(
            row, text="隐藏模式（鼠标移到贴靠热区才显示）", variable=self.hidden_var,
            command=self._apply_options, bg=PANEL_CARD, fg=TEXT,
            selectcolor="#15171d", activebackground=PANEL_CARD,
            activeforeground=TEXT, font=(FONT, px(10)), bd=0,
            highlightthickness=0,
        ).pack(side="left")

        self.hidden_delay_var = tk.StringVar(value="0")
        self.hotspot_var = tk.StringVar(value="160")
        for label, var, lo, hi, inc, tip in (
            ("隐藏延时", self.hidden_delay_var, 0.0, 5.0, 0.5, "秒，0=立即隐藏"),
            ("热区宽度", self.hotspot_var, 60, 600, 20, "px，贴靠区域最小宽度"),
        ):
            row = tk.Frame(right, bg=PANEL_CARD)
            row.pack(fill="x", pady=(0, px(6)))
            self._label(row, label, 10, MUTED, width=8).pack(side="left")
            tk.Spinbox(
                row, from_=lo, to=hi, increment=inc, width=5, textvariable=var,
                bg="#15171d", fg=TEXT, bd=0, relief="flat",
                insertbackground=TEXT, font=(FONT, px(10)),
                command=self._apply_options, buttonbackground="#2a2f3b",
            ).pack(side="left")
            self._label(row, tip, 8, MUTED_DIM).pack(side="left", padx=(px(8), 0))

        tk.Frame(right, bg="#2a2e38", height=1).pack(fill="x", pady=(px(4), px(10)))
        self._label(right, "提示：小组件默认贴靠托盘左侧；拖动后会按相对托盘的偏移记住。"
                    "任务栏空间不足时会自动减少卡片宽度或显示只数。",
                    9, MUTED_DIM, wraplength=px(280), justify="left").pack(anchor="w")
        self._button(right, "恢复默认位置", self.app.reset_position).pack(anchor="w", pady=(px(8), 0))

        # ---- 底部
        footer = tk.Frame(outer, bg=PANEL_BG)
        footer.pack(fill="x", pady=(px(12), 0))
        self.path_label = tk.Label(
            footer, text=f"配置文件：{CONFIG_PATH}", bg=PANEL_BG, fg=MUTED_DIM,
            font=(FONT, px(9)), anchor="w",
        )
        self.path_label.pack(side="left")
        self._button(footer, "退出程序", self.app.quit_app).pack(side="right", padx=(px(8), 0))
        self._button(footer, "打开配置文件", self.app.open_config_file).pack(side="right", padx=(px(8), 0))
        self._button(footer, "保存并应用", self._apply_options).pack(side="right")

    # ----------------------------------------------------------- data binding
    def _load_from_config(self) -> None:
        cfg = self.store.cfg
        display = cfg["display"]
        self.mode_var.set(display["mode"])
        self.count_var.set(str(display["visible_count"]))
        self.style_var.set(display["color_style"])
        self.rotate_var.set(str(display["rotate_seconds"]))
        self.rotate_on_var.set(bool(display.get("rotate_enabled", True)))
        self.interval_var.set(str(cfg["data"]["interval_seconds"]))
        self.width_var.set(str(cfg["window"].get("max_width", 0)))
        self.provider_var.set({"auto": "自动", "tencent": "腾讯", "sina": "新浪"}
                              .get(cfg["data"]["provider"], "自动"))
        self.top_var.set(bool(cfg["window"]["always_on_top"]))
        self.breadth_var.set(bool(display.get("show_breadth", True)))
        self.hidden_var.set(bool(display.get("hidden_mode", False)))
        self.hidden_delay_var.set(str(display.get("hidden_delay", 0)))
        self.hotspot_var.set(str(display.get("hotspot_width", 160)))
        self.scale_var.set(f"{int(round(float(cfg['window'].get('scale', 0.8)) * 100))}%")
        self._refresh_list()

    def _refresh_list(self) -> None:
        quotes, _sparks, meta = self.app.service.snapshot()
        names = meta.get("names") or {}
        selected = self.listbox.curselection()
        index = selected[0] if selected else None
        self.listbox.delete(0, "end")
        for item in self.store.cfg["watchlist"]:
            symbol = item["symbol"]
            quote = quotes.get(symbol)
            kind = str(item.get("kind") or "stock")
            # 中文名优先级：用户别名 → 行情接口中文名 → 本地名称缓存 → 代码
            name = (item.get("alias") or (quote.name if quote else "")
                    or names.get(symbol) or symbol)
            name = self._short_name(name)
            shown = self._display_symbol(symbol)
            if quote:
                price = f"{quote.price:.2f}"
                pct = f"{quote.change_pct:+.2f}%"
                if kind == "gold":
                    state = "手动" if symbol.startswith(GOLD_MANUAL_PREFIX) else "参考"
                    if quote.stale:
                        state += " 离线"
                else:
                    state = "离线" if quote.stale else ""
                line = f"{shown:<12} {name:<9} {price:>9}  {pct:>7}  {state}"
            else:
                line = f"{shown:<12} {name:<9} {'—':>9}"
            self.listbox.insert("end", line)
        if index is not None and self.listbox.size() > 0:
            index = min(index, self.listbox.size() - 1)
            self.listbox.selection_set(index)

    @staticmethod
    def _short_name(name: str, limit: int = 6) -> str:
        text = (name or "").strip()
        return text if len(text) <= limit else text[:limit] + "…"

    @staticmethod
    def _display_symbol(symbol: str) -> str:
        if symbol == GOLD_SGE_SYMBOL:
            return "gold:Au99.99"
        if symbol.startswith(GOLD_MANUAL_PREFIX):
            return "gold:手动"
        return symbol

    # ------------------------------------------------------------------ 搜索
    def _local_name_map(self) -> dict:
        """本地兜底用的名称表：行情缓存 + 用户别名。"""
        _quotes, _sparks, meta = self.app.service.snapshot()
        names = dict(meta.get("names") or {})
        for item in self.store.cfg["watchlist"]:
            alias = (item.get("alias") or "").strip()
            if alias and not names.get(item["symbol"]):
                names[item["symbol"]] = alias
        return names

    def _schedule_search(self) -> None:
        """输入变化后 300ms 再搜，避免每个字符都打一次接口。"""
        if self._search_after is not None:
            try:
                self.win.after_cancel(self._search_after)
            except Exception:
                pass
            self._search_after = None
        self._search_seq += 1              # 每次输入都让在途结果作废
        term = self.search_var.get().strip()
        if not term:
            self._results = []
            self._clear_results()
            self._retry_btn.pack_forget()
            self.search_status.configure(text="输入关键词开始搜索", fg=MUTED_DIM)
            return
        self._search_after = self.win.after(
            300, lambda value=term, seq=self._search_seq: self._start_search(value, seq))

    def _start_search(self, term: str, seq: int | None = None) -> None:
        self._search_after = None
        if seq is None:                    # 重试按钮走这里，重新发号
            self._search_seq += 1
            seq = self._search_seq
        if seq != self._search_seq or term != self.search_var.get().strip():
            return
        self.search_status.configure(text=f"正在搜索「{term}」…", fg=MUTED)
        self._retry_btn.pack_forget()
        self._search_pending += 1
        threading.Thread(
            target=self._search_worker, args=(term, seq), daemon=True
        ).start()
        if self._search_poll is None:
            self._search_poll = self.win.after(100, self._poll_search)

    def _search_worker(self, term: str, seq: int) -> None:
        try:
            rows, error = search_with_fallback(
                term, timeout=5.0, extra_names=self._local_name_map())
        except Exception:
            rows, error = [], "接口不可用"
        self._search_queue.put((seq, term, rows, error))

    def _poll_search(self) -> None:
        """主线程轮询队列，避免在后台线程里直接碰 Tk。"""
        self._search_poll = None
        while True:
            try:
                seq, term, rows, error = self._search_queue.get_nowait()
            except queue.Empty:
                break
            self._search_pending = max(0, self._search_pending - 1)
            if seq == self._search_seq:
                self._show_results(term, rows, error)
        if self._search_pending > 0:
            try:
                if self.win.winfo_exists():
                    self._search_poll = self.win.after(100, self._poll_search)
            except Exception:
                self._search_poll = None

    def _show_results(self, term: str, rows: list, error: str) -> None:
        self._results = rows
        self.result_box.delete(0, "end")
        for item in rows:
            self.result_box.insert(
                "end", f"{item['name']}  ·  {item['code']}  ·  {item['market']}")
        if rows:
            self.result_box.configure(height=min(5, len(rows)))
            if not self._result_visible:
                self.result_wrap.pack(fill="x", pady=(px(4), 0), before=self.sep_ref)
            self._result_visible = True
            note = (f"{error}，以下为本地缓存结果 · 点击或回车添加"
                    if error else
                    f"{len(rows)} 个结果 · 点击或按回车添加第一项")
            self.search_status.configure(text=note, fg=AMBER if error else MUTED)
            if error:
                self._retry_btn.pack(side="left", padx=(px(6), 0))
            return
        self._clear_results()
        if error:
            self.search_status.configure(
                text=f"搜索失败：{error}，本地缓存也没有匹配结果", fg=AMBER)
        else:
            self.search_status.configure(
                text=f"未找到「{term}」，换个名称，或用下方代码输入", fg=AMBER)
        self._retry_btn.pack(side="left", padx=(px(6), 0))

    def _clear_results(self) -> None:
        try:
            self.result_box.delete(0, "end")
            if self._result_visible:
                self.result_wrap.pack_forget()
        except Exception:
            pass
        self._result_visible = False

    def _retry_search(self) -> None:
        term = self.search_var.get().strip()
        if term:
            self._start_search(term)

    def _on_search_enter(self, _event=None) -> str:
        if self._results:
            selected = self.result_box.curselection()
            self._add_candidate(selected[0] if selected else 0)
        elif self.search_var.get().strip():
            self.search_status.configure(text="正在搜索，结果出来后再按回车即可添加", fg=MUTED)
        return "break"

    def _on_result_click(self, event) -> str:
        index = self.result_box.nearest(event.y)
        if 0 <= index < len(self._results):
            self.result_box.selection_clear(0, "end")
            self.result_box.selection_set(index)
            self._add_candidate(index)
        return "break"

    def _on_result_enter(self, _event=None) -> str:
        selected = self.result_box.curselection()
        self._add_candidate(selected[0] if selected else 0)
        return "break"

    # ---------------------------------------------------------------- actions
    def _watched_symbols(self) -> set:
        return {str(item.get("symbol") or "") for item in self.store.cfg["watchlist"]}

    def _append_watch(self, symbol: str, alias: str = "", name: str = "") -> None:
        """代码输入与搜索结果共用的落库/去重后写入路径。"""
        if name:
            self.app.service.remember_name(symbol, name)
        self.store.cfg["watchlist"].append({"symbol": symbol, "alias": alias})
        self.app.apply_config(save=True)
        self._refresh_list()
        self.listbox.see("end")

    # -------------------------------------------------------------- 积存金
    def _open_gold_dialog(self) -> None:
        gold_options = (
            ("工商银行积存金（参考 Au99.99）", "工行积存金", "icbc"),
            ("建设银行积存金（参考 Au99.99）", "建行积存金", "ccb"),
            ("招商银行黄金账户（参考 Au99.99）", "招行黄金账户", "cmb"),
            ("农业银行存金通（参考 Au99.99）", "农行存金通", "abc"),
            ("上海黄金交易所 Au99.99（参考）", "Au99.99 参考", "sge"),
            ("手动价格（自定义，元/克）", "自定义金价", "manual"),
        )
        dlg = tk.Toplevel(self.win)
        dlg.title("添加积存金")
        dlg.configure(bg=PANEL_BG)
        dlg.transient(self.win)
        dlg.geometry(f"{px(460)}x{px(390)}")
        dlg.resizable(False, False)
        body = tk.Frame(dlg, bg=PANEL_BG, padx=px(18), pady=px(14))
        body.pack(fill="both", expand=True)

        self._label(body, "添加积存金", 14, bold=True).pack(anchor="w")
        self._label(body,
                    "工行/建行/招行/农行积存金没有稳定公开报价接口，这里统一取"
                    "上金所 Au99.99 参考价并标注「参考」；也可手动维护固定价格。",
                    9, MUTED_DIM, wraplength=px(410), justify="left").pack(
            anchor="w", pady=(px(2), px(10)))

        choice = tk.StringVar(value=gold_options[0][0])
        option = tk.OptionMenu(body, choice, *(o[0] for o in gold_options))
        option.configure(bg="#15171d", fg=TEXT, bd=0, relief="flat",
                         activebackground="#2a2f3b", highlightthickness=0,
                         font=(FONT, px(10)), anchor="w")
        option["menu"].configure(bg="#20232b", fg=TEXT, bd=0,
                                 activebackground="#3a3f4c",
                                 activeforeground="#ffffff",
                                 font=(FONT, px(10)))
        option.pack(fill="x", pady=(0, px(10)))

        self._label(body, "手动价格（选择「手动价格」时填写）", 10, MUTED,
                    bold=True).pack(anchor="w")
        form = tk.Frame(body, bg=PANEL_BG)
        form.pack(fill="x", pady=(px(4), px(8)))
        name_var = tk.StringVar(value="自定义金价")
        price_var = tk.StringVar(value="")
        prev_var = tk.StringVar(value="")
        for label, var, tip in (
            ("名称", name_var, "显示在卡片上"),
            ("现价", price_var, "元/克，如 865.80"),
            ("昨收/上次", prev_var, "可选，用于计算涨跌幅"),
        ):
            row = tk.Frame(form, bg=PANEL_BG)
            row.pack(fill="x", pady=(0, px(5)))
            self._label(row, label, 10, MUTED, width=8).pack(side="left")
            tk.Entry(row, textvariable=var, bg="#15171d", fg=TEXT, bd=0,
                     relief="flat", insertbackground=TEXT,
                     font=(FONT, px(10))).pack(side="left", fill="x",
                                                        expand=True, ipady=px(4))
            self._label(row, tip, 8, MUTED_DIM).pack(side="left", padx=(px(8), 0))

        note = tk.Label(body, text=GOLD_NOTE, bg=PANEL_BG, fg=MUTED_DIM,
                        font=(FONT, px(8)), anchor="w", wraplength=px(410),
                        justify="left")
        note.pack(fill="x", pady=(0, px(8)))
        result = tk.Label(body, text="", bg=PANEL_BG, fg=AMBER,
                          font=(FONT, px(9)), anchor="w", wraplength=px(410),
                          justify="left")
        result.pack(fill="x")

        def submit() -> None:
            picked = next((o for o in gold_options if o[0] == choice.get()),
                          gold_options[0])
            _label, alias, key = picked
            if key == "manual":
                try:
                    price = float(price_var.get().strip())
                except ValueError:
                    result.configure(text="请填写有效的现价（元/克）")
                    return
                if price <= 0:
                    result.configure(text="现价必须大于 0")
                    return
                try:
                    prev = float(prev_var.get().strip()) if prev_var.get().strip() else 0.0
                except ValueError:
                    result.configure(text="昨收/上次价格格式不正确")
                    return
                alias = (name_var.get().strip() or "自定义金价")[:20]
                self._save_manual_gold(alias, price, prev)
            else:
                self._save_sge_gold(alias, key)
            self.app.apply_config(save=True)
            self._refresh_list()
            dlg.destroy()

        buttons = tk.Frame(body, bg=PANEL_BG)
        buttons.pack(fill="x", pady=(px(10), 0))
        self._button(buttons, "添加", submit).pack(side="right")
        self._button(buttons, "取消", dlg.destroy).pack(side="right", padx=(0, px(8)))
        dlg.grab_set()

    def _save_sge_gold(self, alias: str, key: str) -> None:
        watchlist = self.store.cfg["watchlist"]
        existing = next((i for i in watchlist if i["symbol"] == GOLD_SGE_SYMBOL), None)
        if existing:
            existing["alias"] = alias
            existing["kind"] = "gold"
            self.search_status.configure(
                text=f"参考金价已在自选中，已更新名称为「{alias}」"
                     + ("（银行积存金暂无公开报价）" if key != "sge" else ""),
                fg=MUTED)
            return
        watchlist.append({
            "symbol": GOLD_SGE_SYMBOL,
            "alias": alias,
            "kind": "gold",
            "manual_price": 0,
            "manual_prev": 0,
            "manual_updated": 0,
        })
        self.search_status.configure(
            text=f"已添加 {alias}（上金所 Au99.99 参考价，非该行积存金报价）",
            fg=GREEN)

    def _save_manual_gold(self, alias: str, price: float, prev: float) -> None:
        watchlist = self.store.cfg["watchlist"]
        stamp = time.time()
        existing = next((i for i in watchlist
                         if str(i.get("symbol") or "").startswith(GOLD_MANUAL_PREFIX)
                         and (i.get("alias") or "") == alias), None)
        if existing:
            old_price = float(existing.get("manual_price") or 0)
            if prev <= 0 and old_price > 0 and abs(old_price - price) > 1e-9:
                prev = old_price          # 未填昨收时用上次价格，保持涨跌幅口径
            existing.update({
                "kind": "gold", "manual_price": price, "manual_prev": prev,
                "manual_updated": stamp,
            })
            self.search_status.configure(
                text=f"已更新 {alias} 手动价 {price:.2f} 元/克", fg=GREEN)
            return
        symbol = f"{GOLD_MANUAL_PREFIX}{int(stamp * 1000)}"
        watchlist.append({
            "symbol": symbol, "alias": alias, "kind": "gold",
            "manual_price": price, "manual_prev": prev, "manual_updated": stamp,
        })
        self.search_status.configure(
            text=f"已添加 {alias} 手动价 {price:.2f} 元/克（固定值）", fg=GREEN)

    def _add_candidate(self, index: int) -> None:
        if not (0 <= index < len(self._results)):
            return
        item = self._results[index]
        symbol = item["symbol"]
        name = item.get("name") or ""
        code = item.get("code") or symbol[2:]
        market = item.get("market") or market_label(symbol)
        if symbol in self._watched_symbols():
            self.search_status.configure(
                text=f"{name or code}（{code}）已在自选中，未重复添加", fg=AMBER)
            return
        self._append_watch(symbol, "", name)
        self.search_var.set("")      # 清空搜索框，trace 会同时收起结果
        self.search_status.configure(
            text=f"已添加 {name or code} · {code} · {market}", fg=GREEN)

    def _collect(self) -> None:
        cfg = self.store.cfg
        cfg["display"]["mode"] = self.mode_var.get()
        try:
            cfg["display"]["visible_count"] = int(self.count_var.get())
        except ValueError:
            pass
        cfg["display"]["color_style"] = self.style_var.get()
        try:
            cfg["display"]["rotate_seconds"] = int(float(self.rotate_var.get()))
        except ValueError:
            pass
        cfg["display"]["rotate_enabled"] = bool(self.rotate_on_var.get())
        try:
            cfg["data"]["interval_seconds"] = int(float(self.interval_var.get()))
        except ValueError:
            pass
        cfg["data"]["provider"] = {"自动": "auto", "腾讯": "tencent",
                                   "新浪": "sina"}.get(self.provider_var.get(), "auto")
        try:
            cfg["window"]["max_width"] = max(0, int(float(self.width_var.get())))
        except ValueError:
            pass
        cfg["window"]["always_on_top"] = bool(self.top_var.get())
        cfg["display"]["show_breadth"] = bool(self.breadth_var.get())
        cfg["display"]["hidden_mode"] = bool(self.hidden_var.get())
        try:
            cfg["display"]["hidden_delay"] = max(
                0.0, min(5.0, float(self.hidden_delay_var.get())))
        except ValueError:
            pass
        cfg["display"]["hidden_delay_version"] = 1
        try:
            cfg["display"]["hotspot_width"] = max(
                60, min(600, int(float(self.hotspot_var.get()))))
        except ValueError:
            pass
        try:
            scale = float(self.scale_var.get().strip().rstrip("%")) / 100.0
        except ValueError:
            scale = 0.8
        cfg["window"]["scale"] = round(max(0.6, min(1.4, scale)), 2)

    def _apply_options(self, *_args) -> None:
        if self._refreshing:
            return
        self._collect()
        self.app.apply_config(save=True)
        self._refresh_list()

    def _add_symbol(self) -> None:
        """备用入口：直接输入代码/别名，与搜索共用常量规范化与去重。"""
        raw = self.entry_symbol.get().strip()
        alias = self.entry_alias.get().strip()
        if not raw:
            self.search_status.configure(text="请先填写代码或名称", fg=AMBER)
            return
        self.search_status.configure(text=f"正在解析 {raw} …", fg=MUTED)
        self.win.update_idletasks()
        resolved = resolve_input(raw, timeout=6.0)
        if not resolved:
            self.search_status.configure(
                text=f"未找到「{raw}」，试试 000905 / sh000905 / 中证500", fg=AMBER)
            return
        symbol = resolved["symbol"]
        if symbol in self._watched_symbols():
            self.search_status.configure(
                text=f"{symbol} 已在自选中，未重复添加", fg=AMBER)
            return
        name = resolved.get("name") or ""
        self._append_watch(symbol, alias, name)
        self.entry_symbol.delete(0, "end")
        self.entry_alias.delete(0, "end")
        if resolved.get("price") is not None:
            tip = f"已添加 {symbol} {name} {resolved['price']:.2f} {resolved['pct']:+.2f}%"
        elif name:
            tip = f"已添加 {symbol} {name}（行情暂不可用）"
        else:
            tip = f"已添加 {symbol}（行情暂不可用）"
        self.search_status.configure(text=tip.strip(), fg=MUTED)

    def _remove_selected(self) -> None:
        selected = self.listbox.curselection()
        if not selected:
            return
        index = selected[0]
        if 0 <= index < len(self.store.cfg["watchlist"]):
            self.store.cfg["watchlist"].pop(index)
            self.app.apply_config(save=True)
            self._refresh_list()

    def _move(self, delta: int) -> None:
        selected = self.listbox.curselection()
        if not selected:
            return
        index = selected[0]
        target = index + delta
        watchlist = self.store.cfg["watchlist"]
        if not (0 <= target < len(watchlist)):
            return
        watchlist[index], watchlist[target] = watchlist[target], watchlist[index]
        self.app.apply_config(save=True)
        self.listbox.selection_clear(0, "end")
        self.listbox.selection_set(target)

    # ------------------------------------------------------------------ tick
    def _tick(self) -> None:
        try:
            if self.win.winfo_exists():
                _quotes, _sparks, meta = self.app.service.snapshot()
                text = status_text(meta)
                self.status_label.configure(
                    text=text, fg=AMBER if meta.get("error") else MUTED
                )
                self._refresh_list()
                self.win.after(1200, self._tick)
        except Exception:
            pass

    # -------------------------------------------------------------- visibility
    def show(self) -> None:
        self._refresh_list()
        self.win.deiconify()
        # 设置窗也不进任务栏按钮区，避免它的按钮把小组件挤窄
        apply_tool_window(self.win)
        self.win.lift()
        self.win.attributes("-topmost", True)
        self.win.after(400, lambda: self.win.attributes("-topmost", False))
        try:
            self.win.focus_force()
        except Exception:
            pass

    def hide(self) -> None:
        self.win.withdraw()

    def toggle(self) -> None:
        if self.win.state() == "withdrawn" or not self.win.winfo_viewable():
            self.show()
        else:
            self.hide()
